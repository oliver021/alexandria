import asyncio

from sqlalchemy.exc import IntegrityError

from app.db.engine import init_db
from app.db.session import async_session_factory
from app.models.user import UserRole
from app.repositories.user_repo import create_user


async def seed():
    await init_db()
    async with async_session_factory() as db:
        try:
            await create_user(
                db,
                username="admin",
                email="admin@alexandria.dev",
                password="admin123456",
                role=UserRole.ADMIN,
            )
            await db.commit()
            print("Admin user created: admin / admin123456")
        except IntegrityError as e:
            print(f"Seed skipped: {e}")


if __name__ == "__main__":
    asyncio.run(seed())
