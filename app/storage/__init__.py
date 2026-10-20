from functools import lru_cache

from app.config.settings import settings
from app.storage.base import (
    BLOB_KEY_PATTERN,
    DEFAULT_CHUNK_SIZE,
    BlobIntegrityError,
    BlobNotFound,
    BlobStore,
    BlobStoreError,
    InvalidBlobKey,
    validate_blob_key,
)
from app.storage.local import LocalBlobStore

__all__ = [
    "BLOB_KEY_PATTERN",
    "DEFAULT_CHUNK_SIZE",
    "BlobIntegrityError",
    "BlobNotFound",
    "BlobStore",
    "BlobStoreError",
    "InvalidBlobKey",
    "LocalBlobStore",
    "get_blob_store",
    "reset_blob_store_cache",
    "validate_blob_key",
]


@lru_cache(maxsize=1)
def get_blob_store() -> BlobStore:
    return LocalBlobStore(settings.storage.root)


def reset_blob_store_cache() -> None:
    get_blob_store.cache_clear()
