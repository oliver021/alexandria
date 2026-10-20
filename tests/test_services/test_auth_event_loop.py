from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Self

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import local as local_module
from app.auth import password as password_module
from app.auth.local import LocalAuthProvider
from app.db.base import Base
from app.models.schemas import UserCreate
from app.repositories import user_repo
from app.services.auth_service import AuthService
from app.services.errors import UnauthorizedError

SECRET = "event-loop-signing-secret"
PASSWORD = "password123"
LOOP_BUDGET_SECONDS = 0.1
TICK_SECONDS = 0.005
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def new_user(username: str) -> UserCreate:
    return UserCreate(username=username, email=f"{username}@example.com", password=PASSWORD)


def build_provider() -> LocalAuthProvider:
    return LocalAuthProvider(secret=SECRET, algorithm="HS256", expiry_minutes=60)


def cost_of(bcrypt_hash: str) -> str:
    return bcrypt_hash.split("$")[2]


@pytest.fixture
def isolated_decoy():
    local_module._decoy_password_hash.cache_clear()
    yield
    local_module._decoy_password_hash.cache_clear()


@pytest.fixture
def ready_decoy(isolated_decoy):
    assert password_module.pwd_context.handler("bcrypt").default_rounds == 12
    local_module._decoy_password_hash()


@pytest_asyncio.fixture
async def file_sessions(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'users.db'}", connect_args={"timeout": 60}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class LoopWatch:
    def __init__(self) -> None:
        self.worst_gap = 0.0
        self.ticks = 0
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        last = time.perf_counter()
        while not self._stopping.is_set():
            await asyncio.sleep(TICK_SECONDS)
            now = time.perf_counter()
            self.worst_gap = max(self.worst_gap, now - last)
            self.ticks += 1
            last = now

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(TICK_SECONDS * 4)
        self.worst_gap = 0.0
        self.ticks = 0
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._stopping.set()
        assert self._task is not None
        await self._task


def spy_on(monkeypatch, module, name: str, calls: list[tuple[int, tuple]]) -> None:
    real = getattr(module, name)

    def spy(*args, **kwargs):
        calls.append((threading.get_ident(), args))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)


@pytest.mark.usefixtures("ready_decoy")
class TestTheEventLoopStaysResponsive:
    async def test_while_concurrent_logins_verify_hashes_of_the_production_cost(
        self, file_sessions
    ):
        async with file_sessions() as setup:
            setup_service = AuthService(setup, auth_provider=build_provider())
            await setup_service.create_user(new_user("alice"))
            await setup_service.create_user(new_user("bob"))

        async def attempt(username: str, password: str) -> str:
            async with file_sessions() as session:
                service = AuthService(session, auth_provider=build_provider())
                try:
                    await service.authenticate(username, password)
                except UnauthorizedError:
                    return "rejected"
                return "accepted"

        async with LoopWatch() as watch:
            outcomes = await asyncio.gather(
                attempt("alice", PASSWORD),
                attempt("bob", PASSWORD),
                attempt("alice", "wrong-password"),
                attempt("ghost", PASSWORD),
                attempt("ghost", "another-password"),
                attempt("bob", "wrong-password"),
            )

        assert outcomes == ["accepted", "accepted", "rejected", "rejected", "rejected", "rejected"]
        assert watch.worst_gap < LOOP_BUDGET_SECONDS
        assert watch.ticks > 10

    async def test_while_concurrent_users_are_created_with_the_production_cost(self, file_sessions):
        async def register(index: int) -> None:
            async with file_sessions() as session:
                await AuthService(session, auth_provider=build_provider()).create_user(
                    new_user(f"user-{index}")
                )

        async with LoopWatch() as watch:
            await asyncio.gather(*(register(index) for index in range(4)))

        assert watch.worst_gap < LOOP_BUDGET_SECONDS
        assert watch.ticks > 10

    async def test_the_very_first_unknown_login_is_as_quiet_as_the_next_ones(self, file_sessions):
        provider = build_provider()

        async def attempt(index: int) -> None:
            async with file_sessions() as session:
                service = AuthService(session, auth_provider=provider)
                with pytest.raises(UnauthorizedError):
                    await service.authenticate(f"ghost-{index}", PASSWORD)

        async with LoopWatch() as watch:
            await attempt(0)
            await asyncio.gather(attempt(1), attempt(2))

        assert watch.worst_gap < LOOP_BUDGET_SECONDS


@pytest.mark.usefixtures("fast_password_hashing")
class TestHashingLeavesTheEventLoopThread:
    async def test_creating_a_user_hashes_in_a_worker_thread(self, db_session, monkeypatch):
        calls: list[tuple[int, tuple]] = []
        spy_on(monkeypatch, user_repo, "hash_password", calls)

        await AuthService(db_session, auth_provider=build_provider()).create_user(new_user("alice"))

        assert len(calls) == 1
        assert calls[0][0] != threading.get_ident()

    async def test_the_repository_hashes_in_a_worker_thread(self, db_session, monkeypatch):
        calls: list[tuple[int, tuple]] = []
        spy_on(monkeypatch, user_repo, "hash_password", calls)

        user = await user_repo.create_user(
            db_session, username="carol", email="carol@example.com", password=PASSWORD
        )

        assert user.password_hash.startswith("$2")
        assert len(calls) == 1
        assert calls[0][0] != threading.get_ident()

    @pytest.mark.parametrize("username", ["alice", "ghost"])
    @pytest.mark.parametrize("password", [PASSWORD, "wrong-password"])
    async def test_verification_runs_in_a_worker_thread_for_known_and_unknown_users(
        self, db_session, monkeypatch, username, password
    ):
        service = AuthService(db_session, auth_provider=build_provider())
        await service.create_user(new_user("alice"))
        calls: list[tuple[int, tuple]] = []
        spy_on(monkeypatch, local_module, "verify_password", calls)

        try:
            await service.authenticate(username, password)
        except UnauthorizedError:
            pass

        assert len(calls) == 1
        assert calls[0][0] != threading.get_ident()


@pytest.mark.usefixtures("fast_password_hashing")
class TestTheDecoyHash:
    def test_it_is_computed_when_the_provider_is_built(self, monkeypatch):
        calls: list[tuple[int, tuple]] = []
        spy_on(monkeypatch, local_module, "hash_password", calls)

        build_provider()

        assert len(calls) == 1

    def test_later_providers_reuse_it(self, monkeypatch):
        calls: list[tuple[int, tuple]] = []
        spy_on(monkeypatch, local_module, "hash_password", calls)

        for _ in range(5):
            build_provider()

        assert len(calls) == 1

    @pytest.mark.parametrize("attempts", [1, 2, 5])
    async def test_unknown_logins_never_hash_anything(self, db_session, monkeypatch, attempts):
        provider = build_provider()
        hashed: list[tuple[int, tuple]] = []
        spy_on(monkeypatch, local_module, "hash_password", hashed)

        for index in range(attempts):
            assert await provider.authenticate(db_session, f"ghost-{index}", PASSWORD) is None

        assert hashed == []

    async def test_known_and_unknown_users_take_the_same_path_with_equal_cost(
        self, db_session, monkeypatch
    ):
        provider = build_provider()
        service = AuthService(db_session, auth_provider=provider)
        created = await service.create_user(new_user("alice"))
        stored = (await user_repo.get_user(db_session, created.id)).password_hash
        verified: list[tuple[int, tuple]] = []
        spy_on(monkeypatch, local_module, "verify_password", verified)

        assert await provider.authenticate(db_session, "alice", "wrong-password") is None
        assert await provider.authenticate(db_session, "ghost", "wrong-password") is None
        assert await provider.authenticate(db_session, "ghost", "wrong-password") is None

        known, unknown_first, unknown_second = (args[1] for _, args in verified)
        assert known == stored
        assert unknown_first == unknown_second
        assert unknown_first != stored
        assert cost_of(unknown_first) == cost_of(stored)
        assert threading.get_ident() not in {thread for thread, _ in verified}

    async def test_a_decoy_failure_is_swallowed_like_the_failure_of_a_real_hash(
        self, db_session, monkeypatch
    ):
        provider = build_provider()
        await AuthService(db_session, auth_provider=provider).create_user(new_user("alice"))

        def failing(plain, stored):
            raise ValueError("the hasher refused")

        monkeypatch.setattr(local_module, "verify_password", failing)

        assert await provider.authenticate(db_session, "alice", PASSWORD) is None
        assert await provider.authenticate(db_session, "ghost", PASSWORD) is None


def test_importing_the_provider_module_warms_the_decoy_hash_in_a_fresh_interpreter():
    script = (
        "import app.auth.local as local;"
        "info = local._decoy_password_hash.cache_info();"
        "print(info.currsize, info.misses)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.split() == ["1", "1"]
