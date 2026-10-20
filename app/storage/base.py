from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import BinaryIO, Protocol, runtime_checkable

DEFAULT_CHUNK_SIZE = 64 * 1024
BLOB_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")


class BlobStoreError(Exception):
    pass


class BlobNotFound(BlobStoreError):
    pass


class InvalidBlobKey(BlobStoreError):
    pass


class BlobIntegrityError(BlobStoreError):
    pass


def validate_blob_key(key: str) -> str:
    if not isinstance(key, str) or BLOB_KEY_PATTERN.fullmatch(key) is None:
        raise InvalidBlobKey(f"invalid blob key: {str(key)[:80]!r}")
    return key


@runtime_checkable
class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> bool: ...

    async def get(self, key: str) -> bytes: ...

    async def open(self, key: str) -> BinaryIO: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> bool: ...

    async def size(self, key: str) -> int: ...

    def stream(self, key: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]: ...
