from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from app.storage.base import (
    DEFAULT_CHUNK_SIZE,
    BlobIntegrityError,
    BlobNotFound,
    BlobStoreError,
    validate_blob_key,
)

_PUBLISH_LOCK = threading.Lock()


@contextmanager
def _storage_errors(key: str, action: str) -> Iterator[None]:
    try:
        yield
    except FileNotFoundError as exc:
        raise BlobNotFound(f"blob not found: {key}") from exc
    except OSError as exc:
        raise BlobStoreError(f"could not {action} blob {key}: {exc}") from exc


def _sync_directory(directory: Path) -> None:
    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(os.path.abspath(root))

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        validate_blob_key(key)
        return self._root / key[:2] / f"{key}.zip"

    async def put(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        return await asyncio.to_thread(self._put, key, path, data)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        return await asyncio.to_thread(self._get, key, path)

    async def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        return await asyncio.to_thread(self._open, key, path)

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        return await asyncio.to_thread(self._exists, key, path)

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        return await asyncio.to_thread(self._delete, key, path)

    async def size(self, key: str) -> int:
        path = self._path(key)
        return await asyncio.to_thread(self._size, key, path)

    def stream(self, key: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        path = self._path(key)
        if chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        return self._iterate(key, path, chunk_size)

    async def _iterate(self, key: str, path: Path, chunk_size: int) -> AsyncIterator[bytes]:
        handle = await asyncio.to_thread(self._open, key, path)
        try:
            while True:
                chunk = await asyncio.to_thread(self._read_chunk, key, handle, chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            handle.close()

    @classmethod
    def _put(cls, key: str, path: Path, data: bytes) -> bool:
        if hashlib.sha256(data).hexdigest() != key:
            raise BlobIntegrityError(f"content does not match blob key {key}")
        try:
            if path.is_file():
                return False
            created = cls._write_atomically(key, path, data)
        except OSError as exc:
            raise BlobStoreError(f"could not store blob {key}: {exc}") from exc
        if created:
            _sync_directory(path.parent)
        return created

    @staticmethod
    def _write_atomically(key: str, path: Path, data: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        published = False
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{key[:16]}.", suffix=".tmp", delete=False
            ) as stream:
                temp_name = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            with _PUBLISH_LOCK:
                if path.is_file():
                    return False
                os.replace(temp_name, path)
                published = True
            return True
        finally:
            if temp_name is not None and not published:
                with suppress(FileNotFoundError):
                    os.unlink(temp_name)

    @staticmethod
    def _get(key: str, path: Path) -> bytes:
        with _storage_errors(key, "read"):
            return path.read_bytes()

    @staticmethod
    def _exists(key: str, path: Path) -> bool:
        with _storage_errors(key, "check"):
            return path.is_file()

    @staticmethod
    def _delete(key: str, path: Path) -> bool:
        with _storage_errors(key, "delete"):
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True

    @staticmethod
    def _size(key: str, path: Path) -> int:
        with _storage_errors(key, "stat"):
            return path.stat().st_size

    @staticmethod
    def _open(key: str, path: Path) -> BinaryIO:
        with _storage_errors(key, "open"):
            return path.open("rb")

    @staticmethod
    def _read_chunk(key: str, handle: BinaryIO, chunk_size: int) -> bytes:
        with _storage_errors(key, "read"):
            return handle.read(chunk_size)
