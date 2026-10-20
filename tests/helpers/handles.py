from __future__ import annotations

import errno
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import BinaryIO

from app.storage import LocalBlobStore


class CountingHandle:
    def __init__(self, inner: BinaryIO) -> None:
        self.inner = inner
        self.bytes_read = 0
        self.reads = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        data = self.inner.read(size)
        self.bytes_read += len(data)
        self.reads += 1
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.inner.seek(offset, whence)

    def tell(self) -> int:
        return self.inner.tell()

    def seekable(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True
        self.inner.close()


class FlakyHandle(CountingHandle):
    def __init__(self, inner: BinaryIO, *, fail_read: int | None, fail_seek: int | None) -> None:
        super().__init__(inner)
        self.fail_read = fail_read
        self.fail_seek = fail_seek
        self.seeks = 0

    def read(self, size: int = -1) -> bytes:
        if self.fail_read is not None and self.reads >= self.fail_read:
            raise OSError(errno.EIO, "simulated read failure")
        return super().read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        self.seeks += 1
        if self.fail_seek is not None and self.seeks >= self.fail_seek:
            raise OSError(errno.EIO, "simulated seek failure")
        return super().seek(offset, whence)


class SlowHandle(CountingHandle):
    def __init__(self, inner: BinaryIO, delay: float) -> None:
        super().__init__(inner)
        self.delay = delay
        self.in_flight = threading.Event()

    def read(self, size: int = -1) -> bytes:
        self.in_flight.set()
        time.sleep(self.delay)
        return super().read(size)


class RecordingBlobStore:
    def __init__(
        self,
        inner: LocalBlobStore,
        *,
        wrap: Callable[[BinaryIO, int], BinaryIO] | None = None,
        substitute: tuple[int, str] | None = None,
    ) -> None:
        self.inner = inner
        self.wrap = wrap
        self.substitute = substitute
        self.handles: list[BinaryIO] = []
        self.opened_keys: list[str] = []
        self.whole_reads = 0
        self.streams = 0

    async def put(self, key: str, data: bytes) -> bool:
        return await self.inner.put(key, data)

    async def get(self, key: str) -> bytes:
        self.whole_reads += 1
        return await self.inner.get(key)

    async def open(self, key: str) -> BinaryIO:
        number = len(self.handles)
        if self.substitute is not None and number >= self.substitute[0]:
            key = self.substitute[1]
        self.opened_keys.append(key)
        handle = await self.inner.open(key)
        if self.wrap is not None:
            handle = self.wrap(handle, number)
        self.handles.append(handle)
        return handle

    async def exists(self, key: str) -> bool:
        return await self.inner.exists(key)

    async def delete(self, key: str) -> bool:
        return await self.inner.delete(key)

    async def size(self, key: str) -> int:
        return await self.inner.size(key)

    def stream(self, key: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        self.streams += 1
        return self.inner.stream(key, chunk_size)

    def all_closed(self) -> bool:
        return all(getattr(handle, "closed", False) for handle in self.handles)
