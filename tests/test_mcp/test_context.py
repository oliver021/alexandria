from __future__ import annotations

import pytest

from app import storage
from app.db import session as database
from app.mcp import context
from app.mcp.context import (
    RuntimeConfig,
    configure_runtime,
    get_blob_store,
    get_session_factory,
    reset_runtime,
    session_scope,
)


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self) -> str:
        self.log.append("open")
        return "SESSION"

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.log.append("close")


@pytest.fixture(autouse=True)
def pristine_runtime():
    reset_runtime()
    yield
    reset_runtime()


class TestDefaults:
    def test_without_configuration_the_application_session_factory_is_used(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(database, "async_session_factory", sentinel)

        assert get_session_factory() is sentinel

    def test_without_configuration_the_application_blob_store_is_used(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(storage, "get_blob_store", lambda: sentinel)

        assert get_blob_store() is sentinel

    def test_the_defaults_are_looked_up_on_every_call(self, monkeypatch):
        monkeypatch.setattr(storage, "get_blob_store", lambda: "first")
        first = get_blob_store()
        monkeypatch.setattr(storage, "get_blob_store", lambda: "second")

        assert (first, get_blob_store()) == ("first", "second")

    def test_the_real_defaults_are_the_application_objects(self):
        assert get_session_factory() is database.async_session_factory
        assert get_blob_store() is storage.get_blob_store()


class TestConfiguration:
    def test_both_dependencies_can_be_replaced_together(self):
        factory, store = object(), object()

        configure_runtime(session_factory=factory, blob_store=store)

        assert get_session_factory() is factory
        assert get_blob_store() is store

    def test_updates_are_partial_and_cumulative(self):
        factory, store = object(), object()

        configure_runtime(session_factory=factory)
        configure_runtime(blob_store=store)

        assert (get_session_factory(), get_blob_store()) == (factory, store)

    def test_omitted_arguments_keep_the_previous_value(self):
        factory, other = object(), object()
        configure_runtime(session_factory=factory, blob_store=other)

        configure_runtime()
        configure_runtime(session_factory=None, blob_store=None)

        assert (get_session_factory(), get_blob_store()) == (factory, other)

    def test_a_later_value_replaces_an_earlier_one(self):
        configure_runtime(session_factory="a")
        configure_runtime(session_factory="b")

        assert get_session_factory() == "b"

    def test_reset_restores_the_defaults(self, monkeypatch):
        configure_runtime(session_factory=object(), blob_store=object())
        reset_runtime()
        monkeypatch.setattr(storage, "get_blob_store", lambda: "default-store")

        assert get_session_factory() is database.async_session_factory
        assert get_blob_store() == "default-store"

    def test_the_configuration_is_an_immutable_value(self):
        config = RuntimeConfig(session_factory=None, blob_store=None)

        with pytest.raises(AttributeError):
            config.blob_store = object()
        assert context._runtime == RuntimeConfig()


class TestSessionScope:
    async def test_it_yields_a_session_from_the_configured_factory_and_closes_it(self):
        log: list[str] = []
        configure_runtime(session_factory=lambda: FakeSession(log))

        async with session_scope() as session:
            assert session == "SESSION"
            assert log == ["open"]

        assert log == ["open", "close"]

    async def test_the_session_is_closed_when_the_block_fails(self):
        log: list[str] = []
        configure_runtime(session_factory=lambda: FakeSession(log))

        with pytest.raises(RuntimeError, match="boom"):
            async with session_scope():
                raise RuntimeError("boom")

        assert log == ["open", "close"]

    async def test_every_scope_gets_its_own_session(self):
        log: list[str] = []
        configure_runtime(session_factory=lambda: FakeSession(log))

        async with session_scope():
            pass
        async with session_scope():
            pass

        assert log == ["open", "close", "open", "close"]
