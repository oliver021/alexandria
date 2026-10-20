from __future__ import annotations

import io
import struct
import warnings
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

import yaml

FileMap: TypeAlias = "Mapping[str, str | bytes] | Iterable[tuple[str, str | bytes]]"

FIXED_DATE_TIME = (2020, 1, 1, 0, 0, 0)
DEFAULT_DESCRIPTION = "A sample package used in tests."
DEFAULT_BODY = "# Sample\n\nFollow these instructions.\n"
BYTE_ORDER_MARK = chr(0xFEFF)

ZEROS_SEGMENT_BYTES = 16 * 1024 * 1024
ZEROS_CRC_LIMIT_BYTES = 256 * 1024 * 1024

UNIX_FILE = 0o100644 << 16
UNIX_DIRECTORY = (0o40755 << 16) | 0x10
UNIX_SYMLINK = 0o120777 << 16

_LOCAL_SIGNATURE = 0x04034B50
_CENTRAL_SIGNATURE = 0x02014B50
_END_SIGNATURE = 0x06054B50
_DOS_DATE = ((2020 - 1980) << 9) | (1 << 5) | 1


def _as_bytes(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else bytes(content)


def _file_items(files: FileMap) -> list[tuple[str, str | bytes]]:
    if isinstance(files, Mapping):
        return list(files.items())
    return list(files)


def _member_info(name: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_DATE_TIME)
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = UNIX_DIRECTORY if name.endswith("/") else UNIX_FILE
    return info


def _write_member(archive: zipfile.ZipFile, name: str, content: bytes, compression: int) -> None:
    archive.writestr(_member_info(name, compression), content)


def _parent_directories(paths: Iterable[str]) -> list[str]:
    directories: list[str] = []
    for path in paths:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directory = "/".join(parts[:index]) + "/"
            if directory not in directories:
                directories.append(directory)
    return directories


def make_zip(
    files: FileMap,
    *,
    wrapper: str | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    dir_entries: bool = False,
    macos_junk: bool = False,
) -> bytes:
    prefix = f"{wrapper.strip('/')}/" if wrapper else ""
    items = [(prefix + path, _as_bytes(content)) for path, content in _file_items(files)]
    directories = _parent_directories(name for name, _ in items) if dir_entries else []
    junk: list[tuple[str, bytes]] = []
    if macos_junk:
        junk.append(("__MACOSX/", b""))
        for name, _ in items:
            directory, _, base = name.rpartition("/")
            location = f"{directory}/" if directory else ""
            junk.append((f"__MACOSX/{location}._{base}", b"\x00\x05\x16\x07AppleDouble"))
        junk.append((f"{prefix}.DS_Store", b"\x00\x00\x00\x01Bud1"))
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buffer, "w", compression) as archive:
            for directory in directories:
                _write_member(archive, directory, b"", compression)
            for name, content in items:
                _write_member(archive, name, content, compression)
            for name, content in junk:
                _write_member(archive, name, content, compression)
    return buffer.getvalue()


def frontmatter_doc(block: str, body: str = "", *, eol: str = "\n", bom: bool = False) -> str:
    lines = block.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    joined = "".join(line + eol for line in lines)
    text = f"---{eol}{joined}---{eol}{body.replace(chr(10), eol)}"
    return BYTE_ORDER_MARK + text if bom else text


def _entrypoint_text(
    name: str,
    version: str | None,
    *,
    description: str,
    display_name: str | None,
    tags: Iterable[str] | None,
    category: str | None,
    kind: str | None,
    extra: Mapping[str, Any] | None,
    body: str,
) -> str:
    fields: dict[str, Any] = {"name": name}
    if version is not None:
        fields["version"] = version
    fields["description"] = description
    if display_name is not None:
        fields["display_name"] = display_name
    if tags is not None:
        fields["tags"] = list(tags)
    if category is not None:
        fields["category"] = category
    if kind is not None:
        fields["kind"] = kind
    if extra:
        fields.update(extra)
    block = yaml.safe_dump(
        fields, sort_keys=False, allow_unicode=True, default_flow_style=False, width=10_000
    )
    return f"---\n{block}---\n\n{body}"


def skill_md(
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    *,
    description: str = DEFAULT_DESCRIPTION,
    display_name: str | None = None,
    tags: Iterable[str] | None = None,
    category: str | None = None,
    kind: str | None = None,
    extra: Mapping[str, Any] | None = None,
    body: str = DEFAULT_BODY,
) -> str:
    return _entrypoint_text(
        name,
        version,
        description=description,
        display_name=display_name,
        tags=tags,
        category=category,
        kind=kind,
        extra=extra,
        body=body,
    )


def index_md(
    name: str = "sample-grounding",
    version: str | None = "1.0.0",
    *,
    description: str = DEFAULT_DESCRIPTION,
    display_name: str | None = None,
    tags: Iterable[str] | None = None,
    category: str | None = None,
    kind: str | None = None,
    extra: Mapping[str, Any] | None = None,
    body: str = DEFAULT_BODY,
) -> str:
    return _entrypoint_text(
        name,
        version,
        description=description,
        display_name=display_name,
        tags=tags,
        category=category,
        kind=kind,
        extra=extra,
        body=body,
    )


def make_skill_zip(
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    *,
    description: str = DEFAULT_DESCRIPTION,
    display_name: str | None = None,
    tags: Iterable[str] | None = None,
    category: str | None = None,
    kind: str | None = None,
    extra_frontmatter: Mapping[str, Any] | None = None,
    body: str = DEFAULT_BODY,
    files: FileMap | None = None,
    wrapper: str | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    dir_entries: bool = False,
    macos_junk: bool = False,
) -> bytes:
    entries: dict[str, str | bytes] = {
        "SKILL.md": skill_md(
            name,
            version,
            description=description,
            display_name=display_name,
            tags=tags,
            category=category,
            kind=kind,
            extra=extra_frontmatter,
            body=body,
        )
    }
    if files:
        entries.update(_file_items(files))
    return make_zip(
        entries,
        wrapper=wrapper,
        compression=compression,
        dir_entries=dir_entries,
        macos_junk=macos_junk,
    )


def make_grounding_zip(
    name: str = "sample-grounding",
    version: str | None = "1.0.0",
    *,
    description: str = DEFAULT_DESCRIPTION,
    display_name: str | None = None,
    tags: Iterable[str] | None = None,
    category: str | None = None,
    kind: str | None = None,
    extra_frontmatter: Mapping[str, Any] | None = None,
    body: str = DEFAULT_BODY,
    files: FileMap | None = None,
    wrapper: str | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    dir_entries: bool = False,
    macos_junk: bool = False,
) -> bytes:
    entries: dict[str, str | bytes] = {
        "INDEX.md": index_md(
            name,
            version,
            description=description,
            display_name=display_name,
            tags=tags,
            category=category,
            kind=kind,
            extra=extra_frontmatter,
            body=body,
        )
    }
    if files:
        entries.update(_file_items(files))
    return make_zip(
        entries,
        wrapper=wrapper,
        compression=compression,
        dir_entries=dir_entries,
        macos_junk=macos_junk,
    )


@dataclass
class RawEntry:
    name: str | bytes
    data: bytes = b""
    method: int = zipfile.ZIP_STORED
    crc: int | None = None
    compressed_size: int | None = None
    uncompressed_size: int | None = None
    flags: int | None = None
    external_attr: int = UNIX_FILE
    local_name: str | bytes | None = None
    extra: bytes = b""


def _name_bytes(name: str | bytes) -> bytes:
    return name if isinstance(name, bytes) else name.encode("utf-8")


def _default_flags(name: str | bytes) -> int:
    if isinstance(name, bytes):
        return 0
    return 0x800 if not name.isascii() else 0


def stored_entry(name: str | bytes, content: str | bytes = b"", **overrides: Any) -> RawEntry:
    fields: dict[str, Any] = {"method": zipfile.ZIP_STORED}
    fields.update(overrides)
    return RawEntry(name, _as_bytes(content), **fields)


def deflated_entry(name: str | bytes, content: str | bytes = b"", **overrides: Any) -> RawEntry:
    raw = _as_bytes(content)
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = compressor.compress(raw) + compressor.flush()
    fields: dict[str, Any] = {
        "method": zipfile.ZIP_DEFLATED,
        "crc": zlib.crc32(raw),
        "uncompressed_size": len(raw),
    }
    fields.update(overrides)
    return RawEntry(name, data, **fields)


def build_raw_zip(entries: Iterable[RawEntry], *, comment: bytes = b"") -> bytes:
    body = bytearray()
    directory = bytearray()
    count = 0
    for entry in entries:
        count += 1
        name = _name_bytes(entry.name)
        local_name = name if entry.local_name is None else _name_bytes(entry.local_name)
        flags = entry.flags if entry.flags is not None else _default_flags(entry.name)
        crc = entry.crc if entry.crc is not None else zlib.crc32(entry.data)
        compressed = entry.compressed_size if entry.compressed_size is not None else len(entry.data)
        uncompressed = (
            entry.uncompressed_size if entry.uncompressed_size is not None else len(entry.data)
        )
        offset = len(body)
        body += struct.pack(
            "<IHHHHHIIIHH",
            _LOCAL_SIGNATURE,
            20,
            flags,
            entry.method,
            0,
            _DOS_DATE,
            crc,
            compressed,
            uncompressed,
            len(local_name),
            len(entry.extra),
        )
        body += local_name + entry.extra + entry.data
        directory += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            _CENTRAL_SIGNATURE,
            (3 << 8) | 20,
            20,
            flags,
            entry.method,
            0,
            _DOS_DATE,
            crc,
            compressed,
            uncompressed,
            len(name),
            len(entry.extra),
            0,
            0,
            0,
            entry.external_attr,
            offset,
        )
        directory += name + entry.extra
    directory_offset = len(body)
    end = struct.pack(
        "<IHHHHIIH",
        _END_SIGNATURE,
        0,
        0,
        count,
        count,
        len(directory),
        directory_offset,
        len(comment),
    )
    return bytes(body + directory + end + comment)


def zero_deflate_stream(total_bytes: int, *, segment_bytes: int = ZEROS_SEGMENT_BYTES) -> bytes:
    repeats, rest = divmod(total_bytes, segment_bytes)
    stream = bytearray()
    if repeats:
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        segment = compressor.compress(bytes(segment_bytes)) + compressor.flush(zlib.Z_SYNC_FLUSH)
        stream += segment * repeats
    tail = zlib.compressobj(9, zlib.DEFLATED, -15)
    stream += tail.compress(bytes(rest)) + tail.flush(zlib.Z_FINISH)
    return bytes(stream)


def zeros_crc32(total_bytes: int) -> int:
    chunk = bytes(1 << 20)
    crc = 0
    remaining = total_bytes
    while remaining > 0:
        take = min(remaining, len(chunk))
        crc = zlib.crc32(chunk[:take] if take < len(chunk) else chunk, crc)
        remaining -= take
    return crc


def zero_bomb_entry(
    name: str,
    expanded_bytes: int,
    *,
    declared_size: int | None = None,
    crc: int | None = None,
) -> RawEntry:
    stream = zero_deflate_stream(expanded_bytes)
    declared = expanded_bytes if declared_size is None else declared_size
    if crc is None:
        crc = zeros_crc32(expanded_bytes) if expanded_bytes <= ZEROS_CRC_LIMIT_BYTES else 0
    return RawEntry(
        name,
        stream,
        zipfile.ZIP_DEFLATED,
        crc=crc,
        compressed_size=len(stream),
        uncompressed_size=declared,
    )


def make_zeros_zip(
    expanded_bytes: int,
    *,
    entry_name: str = "payload.bin",
    compression: int = zipfile.ZIP_DEFLATED,
    name: str = "sample-skill",
    version: str | None = "1.0.0",
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        archive.writestr(_member_info("SKILL.md", compression), skill_md(name, version))
        with archive.open(_member_info(entry_name, compression), "w") as stream:
            chunk = bytes(1 << 20)
            remaining = expanded_bytes
            while remaining > 0:
                take = min(remaining, len(chunk))
                stream.write(chunk if take == len(chunk) else chunk[:take])
                remaining -= take
    return buffer.getvalue()


def make_raw_package(
    extra_entries: Iterable[RawEntry],
    *,
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    grounding: bool = False,
    body: str = DEFAULT_BODY,
    entrypoint_prefix: str = "",
) -> bytes:
    entrypoint = "INDEX.md" if grounding else "SKILL.md"
    text = (index_md if grounding else skill_md)(name, version, body=body)
    entries = [stored_entry(entrypoint_prefix + entrypoint, text), *extra_entries]
    return build_raw_zip(entries)


def make_traversal_zip(
    evil_path: str = "../evil.txt",
    *,
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    grounding: bool = False,
) -> bytes:
    return make_raw_package(
        [stored_entry(evil_path, "owned")], name=name, version=version, grounding=grounding
    )


def make_symlink_zip(
    link_name: str = "link",
    target: str = "/etc/passwd",
    *,
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    grounding: bool = False,
) -> bytes:
    return make_raw_package(
        [stored_entry(link_name, target, external_attr=UNIX_SYMLINK)],
        name=name,
        version=version,
        grounding=grounding,
    )


def make_encrypted_zip(
    entry_name: str = "secret.txt",
    *,
    flag_bits: int = 0x1,
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    grounding: bool = False,
) -> bytes:
    return make_raw_package(
        [stored_entry(entry_name, b"\x8f" * 24, flags=flag_bits)],
        name=name,
        version=version,
        grounding=grounding,
    )


def make_bomb_zip(
    expanded_bytes: int,
    *,
    entry_name: str = "payload.bin",
    declared_size: int | None = None,
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    grounding: bool = False,
) -> bytes:
    return make_raw_package(
        [zero_bomb_entry(entry_name, expanded_bytes, declared_size=declared_size)],
        name=name,
        version=version,
        grounding=grounding,
    )


def central_records(data: bytes) -> list[tuple[int, bytes, int]]:
    end = data.rfind(b"PK\x05\x06")
    size, offset = struct.unpack_from("<II", data, end + 12)
    records: list[tuple[int, bytes, int]] = []
    position = offset
    while position < offset + size:
        fields = struct.unpack_from("<IHHHHHHIIIHHHHHII", data, position)
        name_length, extra_length, comment_length = fields[10], fields[11], fields[12]
        name = data[position + 46 : position + 46 + name_length]
        records.append((position, name, fields[16]))
        position += 46 + name_length + extra_length + comment_length
    return records


def _locate(data: bytes, entry_name: str) -> tuple[int, int]:
    target = entry_name.encode("utf-8")
    for central, name, local in central_records(data):
        if name == target:
            return central, local
    raise KeyError(entry_name)


def _patch(data: bytes, patches: Iterable[tuple[int, str, int]]) -> bytes:
    patched = bytearray(data)
    for position, fmt, value in patches:
        struct.pack_into(fmt, patched, position, value)
    return bytes(patched)


def forge_declared_size(data: bytes, entry_name: str, declared_size: int) -> bytes:
    central, local = _locate(data, entry_name)
    return _patch(data, [(central + 24, "<I", declared_size), (local + 22, "<I", declared_size)])


def forge_compressed_size(data: bytes, entry_name: str, declared_size: int) -> bytes:
    central, local = _locate(data, entry_name)
    return _patch(data, [(central + 20, "<I", declared_size), (local + 18, "<I", declared_size)])


def forge_crc(data: bytes, entry_name: str, crc: int) -> bytes:
    central, local = _locate(data, entry_name)
    return _patch(data, [(central + 16, "<I", crc), (local + 14, "<I", crc)])


def set_flag_bits(data: bytes, entry_name: str, flag_bits: int) -> bytes:
    central, local = _locate(data, entry_name)
    return _patch(data, [(central + 8, "<H", flag_bits), (local + 6, "<H", flag_bits)])


def set_compression_method(data: bytes, entry_name: str, method: int) -> bytes:
    central, local = _locate(data, entry_name)
    return _patch(data, [(central + 10, "<H", method), (local + 8, "<H", method)])


def set_external_attr(data: bytes, entry_name: str, external_attr: int) -> bytes:
    central, _ = _locate(data, entry_name)
    return _patch(data, [(central + 38, "<I", external_attr)])


def corrupt_entry_data(data: bytes, entry_name: str, *, index: int = 0) -> bytes:
    _, local = _locate(data, entry_name)
    name_length, extra_length = struct.unpack_from("<HH", data, local + 26)
    position = local + 30 + name_length + extra_length + index
    patched = bytearray(data)
    patched[position] ^= 0xFF
    return bytes(patched)


def set_local_header_offset(data: bytes, entry_name: str, offset: int) -> bytes:
    central, _ = _locate(data, entry_name)
    return _patch(data, [(central + 42, "<I", offset)])


def local_header_offset(data: bytes, entry_name: str) -> int:
    return _locate(data, entry_name)[1]


def unicode_path_extra(original_name: str | bytes, unicode_name: str) -> bytes:
    original = _name_bytes(original_name)
    encoded = unicode_name.encode("utf-8")
    payload = struct.pack("<BL", 1, zlib.crc32(original)) + encoded
    return struct.pack("<HH", 0x7075, len(payload)) + payload


def make_directory_flood_zip(entries: int, *, name: bytes = b"a") -> bytes:
    record = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        _CENTRAL_SIGNATURE,
        (3 << 8) | 20,
        20,
        0,
        0,
        0,
        _DOS_DATE,
        0,
        0,
        0,
        len(name),
        0,
        0,
        0,
        0,
        0,
        0,
    )
    directory = (record + name) * entries
    capped = min(entries, 0xFFFF)
    end = struct.pack("<IHHHHIIH", _END_SIGNATURE, 0, 0, capped, capped, len(directory), 0, 0)
    return directory + end


DESCRIPTOR_SIGNATURE = 0x08074B50
DESCRIPTOR_FLAG = 0x8
ZIP64_MARKER = 0xFFFFFFFF
ALIGNMENT_EXTRA_ID = 0xD935
UNIX_TIMES_EXTRA_ID = 0x5455
UNIX_OWNER_EXTRA_ID = 0x7875
APPLE_DOUBLE_MAGIC = b"\x00\x05\x16\x07"
APPLE_DOUBLE_BODY = APPLE_DOUBLE_MAGIC + bytes(24) + b"Mac OS X        "
DS_STORE_BODY = b"\x00\x00\x00\x01Bud1" + bytes(24)


@dataclass
class LayoutEntry:
    name: str | bytes
    data: bytes = b""
    method: int = zipfile.ZIP_STORED
    crc: int | None = None
    compressed_size: int | None = None
    uncompressed_size: int | None = None
    flags: int | None = None
    external_attr: int = UNIX_FILE
    extra: bytes = b""
    local_extra: bytes | None = None
    central_extra: bytes | None = None
    local_name: str | bytes | None = None
    local_flags: int | None = None
    local_method: int | None = None
    local_crc: int | None = None
    local_compressed_size: int | None = None
    local_uncompressed_size: int | None = None
    streamed: bool = False
    descriptor: bytes | None = None
    gap_after: bytes = b""
    listed: bool = True


def data_descriptor(
    crc: int,
    compressed_size: int,
    uncompressed_size: int,
    *,
    signature: bool = True,
    zip64: bool = False,
) -> bytes:
    sizes = "<QQ" if zip64 else "<II"
    payload = struct.pack("<I", crc) + struct.pack(sizes, compressed_size, uncompressed_size)
    return struct.pack("<I", DESCRIPTOR_SIGNATURE) + payload if signature else payload


def zip64_extra(file_size: int, compress_size: int) -> bytes:
    return struct.pack("<HHQQ", 1, 16, file_size, compress_size)


def layout_stored(name: str | bytes, content: str | bytes = b"", **overrides: Any) -> LayoutEntry:
    return LayoutEntry(name, _as_bytes(content), **overrides)


def layout_deflated(name: str | bytes, content: str | bytes = b"", **overrides: Any) -> LayoutEntry:
    raw = _as_bytes(content)
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = compressor.compress(raw) + compressor.flush()
    fields: dict[str, Any] = {
        "method": zipfile.ZIP_DEFLATED,
        "crc": zlib.crc32(raw),
        "uncompressed_size": len(raw),
    }
    fields.update(overrides)
    return LayoutEntry(name, packed, **fields)


def layout_directory(name: str, **overrides: Any) -> LayoutEntry:
    fields: dict[str, Any] = {"external_attr": UNIX_DIRECTORY}
    fields.update(overrides)
    return LayoutEntry(name if name.endswith("/") else name + "/", b"", **fields)


def _local_value(override: int | None, streamed: bool, actual: int) -> int:
    if override is not None:
        return override
    return 0 if streamed else actual


def layout_local_record(entry: LayoutEntry) -> bytes:
    name = _name_bytes(entry.name)
    local_name = name if entry.local_name is None else _name_bytes(entry.local_name)
    crc = zlib.crc32(entry.data) if entry.crc is None else entry.crc
    compressed = len(entry.data) if entry.compressed_size is None else entry.compressed_size
    uncompressed = len(entry.data) if entry.uncompressed_size is None else entry.uncompressed_size
    flags = _default_flags(entry.name) if entry.flags is None else entry.flags
    if entry.streamed:
        flags |= DESCRIPTOR_FLAG
    local_flags = flags if entry.local_flags is None else entry.local_flags
    local_method = entry.method if entry.local_method is None else entry.local_method
    local_extra = entry.extra if entry.local_extra is None else entry.local_extra
    local_crc = _local_value(entry.local_crc, entry.streamed, crc)
    local_compressed = _local_value(entry.local_compressed_size, entry.streamed, compressed)
    local_uncompressed = _local_value(entry.local_uncompressed_size, entry.streamed, uncompressed)
    descriptor = entry.descriptor
    if descriptor is None:
        descriptor = data_descriptor(crc, compressed, uncompressed) if entry.streamed else b""
    header = struct.pack(
        "<IHHHHHIIIHH",
        _LOCAL_SIGNATURE,
        20,
        local_flags,
        local_method,
        0,
        _DOS_DATE,
        local_crc,
        local_compressed,
        local_uncompressed,
        len(local_name),
        len(local_extra),
    )
    return header + local_name + local_extra + entry.data + descriptor + entry.gap_after


def build_layout_zip(
    entries: Iterable[LayoutEntry],
    *,
    comment: bytes = b"",
    central_order: Sequence[int] | None = None,
) -> bytes:
    body = bytearray()
    records: list[bytes] = []
    for entry in entries:
        offset = len(body)
        body += layout_local_record(entry)
        if not entry.listed:
            continue
        name = _name_bytes(entry.name)
        crc = zlib.crc32(entry.data) if entry.crc is None else entry.crc
        compressed = len(entry.data) if entry.compressed_size is None else entry.compressed_size
        uncompressed = (
            len(entry.data) if entry.uncompressed_size is None else entry.uncompressed_size
        )
        flags = _default_flags(entry.name) if entry.flags is None else entry.flags
        if entry.streamed:
            flags |= DESCRIPTOR_FLAG
        central_extra = entry.extra if entry.central_extra is None else entry.central_extra
        record = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            _CENTRAL_SIGNATURE,
            (3 << 8) | 20,
            20,
            flags,
            entry.method,
            0,
            _DOS_DATE,
            crc,
            compressed,
            uncompressed,
            len(name),
            len(central_extra),
            0,
            0,
            0,
            entry.external_attr,
            offset,
        )
        records.append(record + name + central_extra)
    order = range(len(records)) if central_order is None else central_order
    directory = b"".join(records[index] for index in order)
    end = struct.pack(
        "<IHHHHIIH",
        _END_SIGNATURE,
        0,
        0,
        len(records),
        len(records),
        len(directory),
        len(body),
        len(comment),
    )
    return bytes(body) + directory + end + comment


_LOCAL_PATCHABLE = {
    "name_length": (26, "<H"),
    "extra_length": (28, "<H"),
    "flags": (6, "<H"),
    "method": (8, "<H"),
    "crc": (14, "<I"),
    "compressed_size": (18, "<I"),
    "uncompressed_size": (22, "<I"),
}
_CENTRAL_PATCHABLE = {
    "flags": (8, "<H"),
    "method": (10, "<H"),
    "crc": (16, "<I"),
    "compressed_size": (20, "<I"),
    "uncompressed_size": (24, "<I"),
}


def set_local_fields(data: bytes, entry_name: str, **fields: int) -> bytes:
    _, local = _locate(data, entry_name)
    return _patch(
        data,
        [
            (local + _LOCAL_PATCHABLE[key][0], _LOCAL_PATCHABLE[key][1], value)
            for key, value in fields.items()
        ],
    )


def set_central_fields(data: bytes, entry_name: str, **fields: int) -> bytes:
    central, _ = _locate(data, entry_name)
    return _patch(
        data,
        [
            (central + _CENTRAL_PATCHABLE[key][0], _CENTRAL_PATCHABLE[key][1], value)
            for key, value in fields.items()
        ],
    )


def entry_extent(data: bytes, entry_name: str) -> tuple[int, int]:
    central, local = _locate(data, entry_name)
    compressed = struct.unpack_from("<I", data, central + 20)[0]
    name_length, extra_length = struct.unpack_from("<HH", data, local + 26)
    return local, local + 30 + name_length + extra_length + compressed


def central_directory_offset(data: bytes) -> int:
    end = data.rfind(b"PK\x05\x06")
    return struct.unpack_from("<I", data, end + 16)[0]


def splice_bytes(data: bytes, position: int, payload: bytes) -> bytes:
    end = data.rfind(b"PK\x05\x06")
    directory_offset = struct.unpack_from("<I", data, end + 16)[0]
    shift = len(payload)
    patched = bytearray(data[:position] + payload + data[position:])
    struct.pack_into("<I", patched, end + shift + 16, directory_offset + shift)
    for central, _, local in central_records(data):
        if local >= position:
            struct.pack_into("<I", patched, central + shift + 42, local + shift)
    return bytes(patched)


class _UnseekableBuffer(io.RawIOBase):
    def __init__(self) -> None:
        self._buffer = io.BytesIO()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data: Any) -> int:
        return self._buffer.write(data)

    def tell(self) -> int:
        return self._buffer.tell()

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()


def _archive_plan(
    files: FileMap, wrapper: str | None, dir_entries: bool, macos_junk: bool
) -> tuple[list[str], list[tuple[str, bytes]], list[tuple[str, bytes]]]:
    prefix = f"{wrapper.strip('/')}/" if wrapper else ""
    items = [(prefix + path, _as_bytes(content)) for path, content in _file_items(files)]
    directories = _parent_directories(name for name, _ in items) if dir_entries else []
    junk: list[tuple[str, bytes]] = []
    if macos_junk:
        junk.append(("__MACOSX/", b""))
        for name, _ in items:
            directory, _, base = name.rpartition("/")
            location = f"{directory}/" if directory else ""
            junk.append((f"__MACOSX/{location}._{base}", APPLE_DOUBLE_BODY))
        junk.append((f"{prefix}.DS_Store", DS_STORE_BODY))
    return directories, items, junk


def _write_variant(
    archive: zipfile.ZipFile,
    plan: tuple[list[str], list[tuple[str, bytes]], list[tuple[str, bytes]]],
    compression: int,
    force_zip64: bool,
) -> None:
    directories, items, junk = plan
    for directory in directories:
        _write_member(archive, directory, b"", compression)
    for name, content in [*items, *junk]:
        if force_zip64 and not name.endswith("/"):
            with archive.open(_member_info(name, compression), "w", force_zip64=True) as stream:
                stream.write(content)
        else:
            _write_member(archive, name, content, compression)


def make_stream_zip(
    files: FileMap,
    *,
    wrapper: str | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    dir_entries: bool = False,
    macos_junk: bool = False,
    force_zip64: bool = False,
    comment: bytes = b"",
) -> bytes:
    plan = _archive_plan(files, wrapper, dir_entries, macos_junk)
    target = _UnseekableBuffer()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(target, "w", compression) as archive:
            _write_variant(archive, plan, compression, force_zip64)
            archive.comment = comment
    return target.getvalue()


def make_zip64_zip(
    files: FileMap,
    *,
    wrapper: str | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    dir_entries: bool = False,
    macos_junk: bool = False,
    comment: bytes = b"",
) -> bytes:
    plan = _archive_plan(files, wrapper, dir_entries, macos_junk)
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buffer, "w", compression) as archive:
            _write_variant(archive, plan, compression, True)
            archive.comment = comment
    return buffer.getvalue()


def _unix_times_extra(*, local: bool) -> bytes:
    modified = 1_577_836_800
    if local:
        return struct.pack("<HHBII", UNIX_TIMES_EXTRA_ID, 9, 3, modified, modified)
    return struct.pack("<HHBI", UNIX_TIMES_EXTRA_ID, 5, 3, modified)


def _unix_owner_extra() -> bytes:
    return struct.pack("<HHBBIBI", UNIX_OWNER_EXTRA_ID, 11, 1, 4, 501, 4, 20)


def _macos_member(name: str, content: bytes) -> LayoutEntry:
    return layout_deflated(
        name,
        content,
        streamed=True,
        local_extra=_unix_times_extra(local=True) + _unix_owner_extra(),
        central_extra=_unix_times_extra(local=False) + _unix_owner_extra(),
    )


def _macos_directory(name: str) -> LayoutEntry:
    return layout_directory(
        name,
        local_extra=_unix_times_extra(local=True) + _unix_owner_extra(),
        central_extra=_unix_times_extra(local=False) + _unix_owner_extra(),
    )


def make_macos_zip(files: FileMap, *, wrapper: str = "package", comment: bytes = b"") -> bytes:
    items = [(f"{wrapper}/{path}", _as_bytes(content)) for path, content in _file_items(files)]
    entries: list[LayoutEntry] = [
        _macos_directory(f"{wrapper}/"),
        _macos_directory("__MACOSX/"),
        _macos_directory(f"__MACOSX/{wrapper}/"),
    ]
    seen_directories = {f"{wrapper}/"}
    for name, content in items:
        for directory in _parent_directories([name]):
            if directory not in seen_directories:
                seen_directories.add(directory)
                entries.append(_macos_directory(directory))
                entries.append(_macos_directory(f"__MACOSX/{directory}"))
        entries.append(_macos_member(name, content))
        folder, _, base = name.rpartition("/")
        entries.append(_macos_member(f"__MACOSX/{folder}/._{base}", APPLE_DOUBLE_BODY))
    entries.append(_macos_member(f"{wrapper}/.DS_Store", DS_STORE_BODY))
    entries.append(_macos_member(f"__MACOSX/{wrapper}/._.DS_Store", APPLE_DOUBLE_BODY))
    return build_layout_zip(entries, comment=comment)


def _alignment_padding(position: int, alignment: int) -> bytes:
    shortfall = (-position) % alignment
    if not shortfall:
        return b""
    while shortfall < 6:
        shortfall += alignment
    return struct.pack("<HHH", ALIGNMENT_EXTRA_ID, shortfall - 4, alignment) + bytes(shortfall - 6)


def make_zipalign_zip(files: FileMap, *, alignment: int = 4, comment: bytes = b"") -> bytes:
    entries: list[LayoutEntry] = []
    offset = 0
    for name, content in _file_items(files):
        data = _as_bytes(content)
        padding = _alignment_padding(offset + 30 + len(_name_bytes(name)), alignment)
        entry = layout_stored(name, data, local_extra=padding, central_extra=b"")
        entries.append(entry)
        offset += len(layout_local_record(entry))
    return build_layout_zip(entries, comment=comment)


def streaming_names(data: bytes) -> list[str]:
    names: list[str] = []
    position = 0
    while data[position : position + 4] == b"PK\x03\x04":
        fields = struct.unpack_from("<IHHHHHIIIHH", data, position)
        flags, compressed, name_length, extra_length = fields[2], fields[7], fields[9], fields[10]
        raw = data[position + 30 : position + 30 + name_length]
        names.append(raw.decode("utf-8" if flags & 0x800 else "cp437"))
        position += 30 + name_length + extra_length + compressed
    return names


def layout_bomb(
    name: str, expanded_bytes: int, *, declared_size: int = 1, **overrides: Any
) -> LayoutEntry:
    stream = zero_deflate_stream(expanded_bytes)
    fields: dict[str, Any] = {
        "method": zipfile.ZIP_DEFLATED,
        "crc": 0,
        "uncompressed_size": declared_size,
    }
    fields.update(overrides)
    return LayoutEntry(name, stream, **fields)


ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP64_END_SIGNATURE = b"PK\x06\x06"
ZIP64_LOCATOR_SIZE = 20
ZIP64_END_SIZE = 56
CENTRAL_LENGTH_FIELDS = {"name": 28, "extra": 30, "comment": 32}
END_FIELD_FORMATS = {
    "disk": (4, "<H"),
    "directory_disk": (6, "<H"),
    "entries_here": (8, "<H"),
    "entries": (10, "<H"),
    "size": (12, "<I"),
    "offset": (16, "<I"),
}
END_FIELD_MARKERS = {
    "disk": 0xFFFF,
    "directory_disk": 0xFFFF,
    "entries_here": 0xFFFF,
    "entries": 0xFFFF,
    "size": 0xFFFFFFFF,
    "offset": 0xFFFFFFFF,
}


def end_record_offset(data: bytes) -> int:
    return data.rfind(b"PK\x05\x06")


def end_fields(data: bytes) -> dict[str, int]:
    end = end_record_offset(data)
    return {
        key: struct.unpack_from(fmt, data, end + position)[0]
        for key, (position, fmt) in END_FIELD_FORMATS.items()
    }


def set_end_fields(data: bytes, **fields: int) -> bytes:
    end = end_record_offset(data)
    return _patch(
        data,
        [
            (end + END_FIELD_FORMATS[key][0], END_FIELD_FORMATS[key][1], value)
            for key, value in fields.items()
        ],
    )


def has_zip64_end(data: bytes) -> bool:
    locator = end_record_offset(data) - ZIP64_LOCATOR_SIZE
    return locator >= 0 and data[locator : locator + 4] == ZIP64_LOCATOR_SIGNATURE


def central_directory_stop(data: bytes) -> int:
    end = end_record_offset(data)
    if has_zip64_end(data):
        return end - ZIP64_LOCATOR_SIZE - ZIP64_END_SIZE
    return end


def with_zip64_end(
    data: bytes,
    *,
    markers: Iterable[str] = (),
    wide: Mapping[str, int] | None = None,
    narrow: Mapping[str, int] | None = None,
    locator_offset: int | None = None,
    locator_disk: int = 0,
    disks: int = 1,
    record: bool = True,
    record_signature: bytes = ZIP64_END_SIGNATURE,
    record_size: int = ZIP64_END_SIZE - 12,
) -> bytes:
    end = end_record_offset(data)
    values = {**end_fields(data), **(wide or {})}
    wide_record = struct.pack(
        "<4sQHHIIQQQQ",
        record_signature,
        record_size,
        45,
        45,
        values["disk"],
        values["directory_disk"],
        values["entries_here"],
        values["entries"],
        values["size"],
        values["offset"],
    )
    locator = struct.pack(
        "<4sIQI",
        ZIP64_LOCATOR_SIGNATURE,
        locator_disk,
        end if locator_offset is None else locator_offset,
        disks,
    )
    narrowed = {key: END_FIELD_MARKERS[key] for key in markers}
    narrowed.update(narrow or {})
    tail = bytearray(data[end:])
    for key, value in narrowed.items():
        position, fmt = END_FIELD_FORMATS[key]
        struct.pack_into(fmt, tail, position, value)
    return data[:end] + (wide_record if record else b"") + locator + bytes(tail)


def central_directory_start(data: bytes) -> int:
    if has_zip64_end(data):
        return struct.unpack_from("<4sQHHIIQQQQ", data, central_directory_stop(data))[9]
    return struct.unpack_from("<I", data, end_record_offset(data) + 16)[0]


def last_central_record(data: bytes) -> int:
    position, last, stop = central_directory_start(data), -1, central_directory_stop(data)
    while position < stop:
        last = position
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", data, position + 28)
        position += 46 + name_length + extra_length + comment_length
    return last


def overflow_central_directory(
    data: bytes, *, field: str = "name", tail: bytes = b"/../../e"
) -> bytes:
    stop = central_directory_stop(data)
    position = last_central_record(data) + CENTRAL_LENGTH_FIELDS[field]
    current = struct.unpack_from("<H", data, position)[0]
    patched = bytearray(data)
    patched[stop + 4 : stop + 4 + len(tail)] = tail
    struct.pack_into("<H", patched, position, current + 4 + len(tail))
    return bytes(patched)


def length_honouring_names(data: bytes, count: int) -> list[bytes]:
    position = central_directory_start(data)
    names: list[bytes] = []
    for _ in range(count):
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", data, position + 28)
        names.append(data[position + 46 : position + 46 + name_length])
        position += 46 + name_length + extra_length + comment_length
    return names


def deflate_bytes(content: str | bytes, *, level: int = 9, final: bool = True) -> bytes:
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    packed = compressor.compress(_as_bytes(content))
    return packed + compressor.flush(zlib.Z_FINISH if final else zlib.Z_SYNC_FLUSH)


def deflate_carrier(
    name: str,
    content: str | bytes,
    slack: bytes = b"",
    *,
    stream: bytes | None = None,
    **overrides: Any,
) -> LayoutEntry:
    raw = _as_bytes(content)
    packed = (deflate_bytes(raw) if stream is None else stream) + slack
    fields: dict[str, Any] = {
        "method": zipfile.ZIP_DEFLATED,
        "crc": zlib.crc32(raw),
        "uncompressed_size": len(raw),
    }
    fields.update(overrides)
    return LayoutEntry(name, packed, **fields)


def deflate_streaming_walk(data: bytes) -> tuple[list[str], int] | None:
    names: list[str] = []
    position = 0
    try:
        while data[position : position + 4] == b"PK\x03\x04":
            fields = struct.unpack_from("<IHHHHHIIIHH", data, position)
            flags, method, compressed = fields[2], fields[3], fields[7]
            name_length, extra_length = fields[9], fields[10]
            raw = data[position + 30 : position + 30 + name_length]
            names.append(raw.decode("utf-8" if flags & 0x800 else "cp437"))
            start = position + 30 + name_length + extra_length
            if method == zipfile.ZIP_DEFLATED and (compressed or flags & DESCRIPTOR_FLAG):
                inflater = zlib.decompressobj(-15)
                inflater.decompress(data[start:])
                if not inflater.eof:
                    return None
                end = len(data) - len(inflater.unused_data)
            elif flags & DESCRIPTOR_FLAG and method != zipfile.ZIP_DEFLATED:
                return None
            else:
                end = start + compressed
            if flags & DESCRIPTOR_FLAG:
                consumed = end - start
                cursor = end + 4 if data[end : end + 4] == b"PK\x07\x08" else end
                if struct.unpack_from("<II", data, cursor + 4)[0] == consumed:
                    cursor += 12
                elif struct.unpack_from("<Q", data, cursor + 4)[0] == consumed:
                    cursor += 20
                else:
                    return None
                end = cursor
            position = end
    except (struct.error, zlib.error, UnicodeDecodeError):
        return None
    return names, position
