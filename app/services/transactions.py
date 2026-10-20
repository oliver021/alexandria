from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def write_transaction(db: AsyncSession) -> AsyncIterator[None]:
    try:
        yield
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
