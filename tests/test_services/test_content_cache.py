from __future__ import annotations

import gc
import json
import random
import tracemalloc
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.config.settings import settings
from app.models.schemas import ManifestEntryResponse
from app.repositories import skill_repo
from app.services import mappers
from app.services.errors import StorageError
from app.services.package_service import (
    CACHE_ENTRY_OVERHEAD,
    CONTENT_CACHE_BYTES,
    MANIFEST_COST_FACTOR,
    ContentCache,
    PackageService,
    shared_cache,
)
from app.storage import LocalBlobStore
from tests.helpers.handles import RecordingBlobStore
from tests.helpers.packages import corrupt_entry_data, make_skill_zip

NAME = "cached-skill"
FILES = {
    "docs/guide.md": "# Guide\n\nHello there\n",
    "scripts/run.py": "print('hi')\n",
    "data/table.bin": bytes(range(256)),
    "notes/ünïcode.txt": "héllo wörld\n",
    "empty.txt": "",
}


def blob_file(store: LocalBlobStore, sha256: str):
    return store.root / sha256[:2] / f"{sha256}.zip"


def new_service(db, store, **options) -> PackageService:
    return PackageService(db, store, public_read=True, auto_publish=False, **options)


@pytest.fixture
def store(blob_store: LocalBlobStore) -> RecordingBlobStore:
    return RecordingBlobStore(blob_store)


@pytest.fixture
def service(db_session, store) -> PackageService:
    return new_service(db_session, store)


@pytest_asyncio.fixture
async def docs(service, actors):
    return await service.upload(
        actors.owner, make_skill_zip(NAME, "1.0.0", files=FILES), publish=True
    )


async def tamper(db_session, version: str, **fields) -> None:
    skill = await skill_repo.get_skill_by_name(db_session, NAME)
    target = await skill_repo.get_version(db_session, skill.id, version)
    for key, value in fields.items():
        setattr(target, key, value)
    await db_session.commit()


class TestTheBudget:
    def test_the_default_budget_is_thirty_two_mebibytes(self):
        assert CONTENT_CACHE_BYTES == 32 * 1024 * 1024
        assert ContentCache().max_bytes == CONTENT_CACHE_BYTES

    def test_a_stored_value_comes_back_and_a_missing_key_does_not(self):
        cache = ContentCache(10_000)

        cache.put("a", "value", 10)

        assert cache.get("a") == "value"
        assert cache.get("b") is None
        assert len(cache) == 1

    def test_falsy_values_are_real_values(self):
        cache = ContentCache(10_000)

        cache.put("empty", "", 0)

        assert cache.get("empty") == ""

    def test_every_entry_is_charged_its_cost_plus_a_fixed_overhead(self):
        cache = ContentCache(10_000)

        cache.put("a", "x", 100)
        cache.put("b", "y", 250)

        assert cache.total_bytes == 100 + 250 + 2 * CACHE_ENTRY_OVERHEAD

    def test_the_least_recently_used_entry_is_the_first_to_go(self):
        charge = 100 + CACHE_ENTRY_OVERHEAD
        cache = ContentCache(3 * charge)
        for key in "abc":
            cache.put(key, key, 100)

        assert cache.get("a") == "a"
        cache.put("d", "d", 100)

        assert cache.get("b") is None
        assert [cache.get(key) for key in "acd"] == ["a", "c", "d"]
        assert cache.total_bytes == 3 * charge

    def test_a_big_entry_evicts_as_many_small_ones_as_it_needs(self):
        small = 50 + CACHE_ENTRY_OVERHEAD
        cache = ContentCache(4 * small)
        for key in "abcd":
            cache.put(key, key, 50)

        cache.put("big", "big", 2 * small - CACHE_ENTRY_OVERHEAD)

        assert cache.get("a") is None and cache.get("b") is None
        assert cache.get("c") == "c" and cache.get("d") == "d" and cache.get("big") == "big"
        assert cache.total_bytes <= cache.max_bytes

    def test_replacing_a_key_replaces_its_charge(self):
        cache = ContentCache(10_000)
        cache.put("a", "old", 400)

        cache.put("a", "new", 100)

        assert cache.get("a") == "new"
        assert len(cache) == 1
        assert cache.total_bytes == 100 + CACHE_ENTRY_OVERHEAD

    def test_a_value_that_cannot_fit_is_not_stored_and_displaces_nobody(self):
        cache = ContentCache(2_000)
        cache.put("keep", "keep", 100)

        cache.put("huge", "huge", 5_000)

        assert cache.get("huge") is None
        assert cache.get("keep") == "keep"
        assert cache.total_bytes == 100 + CACHE_ENTRY_OVERHEAD

    def test_a_value_that_no_longer_fits_also_drops_the_stale_one_under_its_key(self):
        cache = ContentCache(2_000)
        cache.put("a", "small", 100)

        cache.put("a", "huge", 5_000)

        assert cache.get("a") is None
        assert cache.total_bytes == 0

    def test_a_budget_of_zero_stores_nothing(self):
        cache = ContentCache(0)

        cache.put("a", "x", 0)

        assert cache.get("a") is None
        assert len(cache) == 0 and cache.total_bytes == 0

    def test_a_negative_budget_is_treated_as_zero(self):
        cache = ContentCache(-5)

        cache.put("a", "x", 0)

        assert cache.max_bytes == 0 and cache.get("a") is None

    def test_clearing_empties_the_cache_and_its_account(self):
        cache = ContentCache(10_000)
        cache.put("a", "x", 10)

        cache.clear()

        assert len(cache) == 0 and cache.total_bytes == 0 and cache.get("a") is None

    def test_the_budget_is_never_exceeded_under_a_random_workload(self):
        generator = random.Random(20261016)
        cache = ContentCache(5_000)
        costs = [0, 10, 100, 700, 1_500, 4_000, 9_000]
        for step in range(4_000):
            if generator.random() < 0.3:
                cache.get(generator.randrange(60))
            key, cost = generator.randrange(60), generator.choice(costs)
            cache.put(key, step, cost)

            assert cache.total_bytes <= cache.max_bytes
            assert len(cache) <= cache.max_bytes // CACHE_ENTRY_OVERHEAD
            fits = cost + CACHE_ENTRY_OVERHEAD <= cache.max_bytes
            assert (cache.get(key) == step) is fits
        cache.clear()
        assert cache.total_bytes == 0

    def test_threads_hammering_the_cache_keep_its_account_exact(self):
        cache = ContentCache(20_000)

        def work(seed: int) -> None:
            generator = random.Random(seed)
            for step in range(3_000):
                key = generator.randrange(40)
                if generator.random() < 0.5:
                    cache.get(key)
                else:
                    cache.put(key, step, generator.choice([0, 50, 400, 3_000]))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(8)))

        assert cache.total_bytes <= cache.max_bytes
        cache.clear()
        assert cache.total_bytes == 0 and len(cache) == 0


class TestOneCachePerStore:
    def test_the_same_store_always_gets_the_same_cache(self, blob_store):
        assert shared_cache(blob_store) is shared_cache(blob_store)

    def test_other_stores_do_not_share_it(self, blob_store, tmp_path):
        other = LocalBlobStore(tmp_path / "elsewhere")

        assert shared_cache(other) is not shared_cache(blob_store)

    def test_the_cache_of_a_store_has_the_default_budget(self, blob_store):
        assert shared_cache(blob_store).max_bytes == CONTENT_CACHE_BYTES

    def test_a_store_that_cannot_be_weakly_referenced_simply_gets_no_cache(self):
        class Slotted:
            __slots__ = ()

        cache = shared_cache(Slotted())
        cache.put("key", "value", 1)

        assert cache.get("key") is None

    def test_a_store_that_cannot_be_hashed_simply_gets_no_cache(self):
        class Unhashable:
            __hash__ = None

        cache = shared_cache(Unhashable())
        cache.put("key", "value", 1)

        assert cache.get("key") is None

    def test_the_cache_goes_away_with_its_store(self, tmp_path):
        store = LocalBlobStore(tmp_path / "ephemeral")
        reference = weakref.ref(shared_cache(store))

        del store
        gc.collect()

        assert reference() is None

    def test_a_service_uses_the_cache_of_its_store_unless_it_is_given_another(
        self, db_session, blob_store
    ):
        own = ContentCache(1_000)

        assert new_service(db_session, blob_store).cache is shared_cache(blob_store)
        assert new_service(db_session, blob_store, cache=own).cache is own


class TestFilesAreReadOnce:
    async def test_the_second_read_of_the_entry_point_does_not_touch_the_store(
        self, service, store, docs
    ):
        first = await service.entrypoint(None, NAME, "latest")
        assert len(store.opened_keys) == 1

        second = await service.entrypoint(None, NAME, "latest")

        assert len(store.opened_keys) == 1
        assert second == first
        assert first.content.startswith("---")

    async def test_the_blob_is_not_even_opened_for_the_hundredth_read(self, service, store, docs):
        for _ in range(100):
            await service.entrypoint(None, NAME, "1.0.0")

        assert len(store.opened_keys) == 1
        assert store.whole_reads == 0 and store.streams == 0

    async def test_latest_and_the_exact_version_share_the_cached_body(self, service, store, docs):
        by_alias = await service.entrypoint(None, NAME, "latest")
        by_number = await service.entrypoint(None, NAME, "1.0.0")

        assert len(store.opened_keys) == 1
        assert by_alias == by_number

    async def test_every_other_file_is_read_once_and_then_remembered(self, service, store, docs):
        await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        await service.read_file(None, NAME, "1.0.0", "scripts/run.py")
        assert len(store.opened_keys) == 2

        guide = await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        run = await service.read_file(None, NAME, "1.0.0", "scripts/run.py")
        unicode_name = await service.read_file(None, NAME, "1.0.0", "notes/ünïcode.txt")

        assert len(store.opened_keys) == 3
        assert guide.content == FILES["docs/guide.md"]
        assert run.content == FILES["scripts/run.py"]
        assert unicode_name.content == FILES["notes/ünïcode.txt"]

    async def test_an_empty_file_is_cached_like_any_other(self, service, store, docs):
        first = await service.read_file(None, NAME, "1.0.0", "empty.txt")
        second = await service.read_file(None, NAME, "1.0.0", "empty.txt")

        assert first.content == second.content == ""
        assert len(store.opened_keys) == 1

    async def test_binary_files_never_open_the_blob_and_never_enter_the_cache(
        self, service, store, docs
    ):
        for _ in range(2):
            binary = await service.read_file(None, NAME, "1.0.0", "data/table.bin")

        assert binary.content is None and binary.text is False
        assert store.opened_keys == []
        assert len(service.cache) == 1

    async def test_what_comes_back_is_a_private_copy_of_the_cached_text(self, service, store, docs):
        first = await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        first.content = "tampered by a caller"
        first.truncated = True

        second = await service.read_file(None, NAME, "1.0.0", "docs/guide.md")

        assert second.content == FILES["docs/guide.md"]
        assert second.truncated is False

    async def test_the_entry_point_and_a_plain_read_of_it_agree(self, service, store, docs):
        entry = await service.entrypoint(None, NAME, "1.0.0")
        plain = await service.read_file(None, NAME, "1.0.0", "SKILL.md")

        assert len(store.opened_keys) == 1
        assert entry.content == plain.content
        assert (entry.sha256, entry.size) == (plain.sha256, plain.size)

    async def test_a_truncated_read_is_remembered_with_its_own_limit(
        self, db_session, store, docs, service
    ):
        short = new_service(db_session, store, max_inline_read_bytes=10)
        full = new_service(db_session, store)

        cut = await short.read_file(None, NAME, "1.0.0", "docs/guide.md")
        whole = await full.read_file(None, NAME, "1.0.0", "docs/guide.md")
        again_cut = await short.read_file(None, NAME, "1.0.0", "docs/guide.md")
        again_whole = await full.read_file(None, NAME, "1.0.0", "docs/guide.md")

        assert cut.truncated and cut.content == FILES["docs/guide.md"][:10]
        assert not whole.truncated and whole.content == FILES["docs/guide.md"]
        assert (again_cut, again_whole) == (cut, whole)
        assert len(store.opened_keys) == 2

    async def test_the_raw_stream_is_not_cached(self, service, store, docs):
        for _ in range(2):
            raw = await service.open_raw(None, NAME, "1.0.0", "docs/guide.md")
            assert (
                b"".join([chunk async for chunk in raw.stream]) == FILES["docs/guide.md"].encode()
            )

        assert len(store.opened_keys) == 4


class TestTheManifestIsParsedOnce:
    @pytest.fixture
    def parsed(self, monkeypatch):
        calls: list[str] = []
        real = mappers.manifest_entries

        def counting(version):
            calls.append(version.id)
            return real(version)

        monkeypatch.setattr(mappers, "manifest_entries", counting)
        return calls

    async def test_listing_reading_and_streaming_share_one_parse(
        self, service, store, docs, parsed
    ):
        for _ in range(3):
            await service.manifest(None, NAME, "1.0.0")
        await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        await service.entrypoint(None, NAME, "latest")
        raw = await service.open_raw(None, NAME, "1.0.0", "data/table.bin")
        await raw.stream.aclose()

        assert len(parsed) == 1

    async def test_each_listing_is_a_list_of_its_own(self, service, docs, parsed):
        first = await service.manifest(None, NAME, "1.0.0")
        first.files.clear()

        second = await service.manifest(None, NAME, "1.0.0")

        assert len(second.files) == len(FILES) + 1
        assert all(isinstance(entry, ManifestEntryResponse) for entry in second.files)
        assert [entry.path for entry in second.files] == sorted(
            entry.path for entry in second.files
        )

    async def test_services_built_for_the_same_store_share_the_parse(
        self, db_session, store, service, docs, parsed
    ):
        await service.manifest(None, NAME, "1.0.0")
        later = new_service(db_session, store)

        listing = await later.manifest(None, NAME, "1.0.0")

        assert len(parsed) == 1
        assert len(listing.files) == len(FILES) + 1

    async def test_another_store_parses_for_itself(
        self, db_session, blob_store, service, docs, parsed
    ):
        await service.manifest(None, NAME, "1.0.0")
        other = new_service(db_session, RecordingBlobStore(blob_store))

        await other.manifest(None, NAME, "1.0.0")

        assert len(parsed) == 2

    async def test_a_changed_manifest_text_is_parsed_again(self, db_session, service, docs, parsed):
        listing = await service.manifest(None, NAME, "1.0.0")
        entries = [entry.model_dump() for entry in listing.files]
        entries[0]["media_type"] = "text/x-changed"
        await tamper(db_session, "1.0.0", manifest=json.dumps(entries))

        changed = await service.manifest(None, NAME, "1.0.0")

        assert len(parsed) == 2
        assert changed.files[0].media_type == "text/x-changed"

    @pytest.mark.parametrize("column", ["{not json", '{"a": 1}', "[1, 2]"])
    async def test_an_unreadable_manifest_is_an_error_every_time_and_is_never_cached(
        self, db_session, service, docs, parsed, column
    ):
        await service.manifest(None, NAME, "1.0.0")
        await tamper(db_session, "1.0.0", manifest=column)

        for _ in range(2):
            with pytest.raises(StorageError):
                await service.manifest(None, NAME, "1.0.0")
            with pytest.raises(StorageError):
                await service.read_file(None, NAME, "1.0.0", "docs/guide.md")

        assert len(parsed) == 5

    def test_the_charge_for_a_manifest_covers_the_memory_it_really_takes(self):
        entries = [
            {
                "path": f"references/section-{number:04d}/notes/file-{number:04d}.md",
                "size": 1000 + number,
                "sha256": f"{number:064x}",
                "media_type": "text/markdown",
                "text": True,
            }
            for number in range(2000)
        ]
        raw = json.dumps(entries)
        holder = SimpleNamespace(id="version", manifest=raw)
        tracemalloc.start()
        try:
            before = tracemalloc.take_snapshot()
            parsed = tuple(mappers.manifest_entries(holder))
            after = tracemalloc.take_snapshot()
        finally:
            tracemalloc.stop()
        measured = sum(stat.size_diff for stat in after.compare_to(before, "filename"))

        assert len(parsed) == 2000
        assert measured + len(raw) <= MANIFEST_COST_FACTOR * len(raw)


class TestVerificationHappensBeforeAnythingIsRemembered:
    async def test_a_damaged_member_is_an_error_every_time_and_is_never_cached(
        self, service, store, blob_store, docs
    ):
        path = blob_file(blob_store, docs.version.sha256)
        path.write_bytes(corrupt_entry_data(path.read_bytes(), "docs/guide.md"))

        for _ in range(3):
            with pytest.raises(StorageError):
                await service.read_file(None, NAME, "1.0.0", "docs/guide.md")

        assert len(store.opened_keys) == 3

    async def test_a_blob_that_is_not_a_zip_is_an_error_every_time(
        self, service, store, blob_store, docs
    ):
        blob_file(blob_store, docs.version.sha256).write_bytes(b"garbage")

        for _ in range(2):
            with pytest.raises(StorageError):
                await service.entrypoint(None, NAME, "1.0.0")

        assert len(store.opened_keys) == 2

    async def test_a_missing_blob_is_an_error_every_time_for_files_nobody_read_yet(
        self, service, store, blob_store, docs
    ):
        await blob_store.delete(docs.version.sha256)

        for _ in range(2):
            with pytest.raises(StorageError):
                await service.read_file(None, NAME, "1.0.0", "docs/guide.md")

    async def test_a_hash_in_the_manifest_that_disagrees_with_the_blob_is_never_served_warm(
        self, db_session, service, store, docs
    ):
        warm = await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        listing = await service.manifest(None, NAME, "1.0.0")
        entries = [entry.model_dump() for entry in listing.files]
        for entry in entries:
            if entry["path"] == "docs/guide.md":
                entry["sha256"] = "0" * 64
        await tamper(db_session, "1.0.0", manifest=json.dumps(entries))

        with pytest.raises(StorageError):
            await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        untouched = await service.read_file(None, NAME, "1.0.0", "scripts/run.py")

        assert warm.content == FILES["docs/guide.md"]
        assert untouched.content == FILES["scripts/run.py"]

    async def test_a_size_in_the_manifest_that_disagrees_with_the_blob_is_never_served_warm(
        self, db_session, service, docs
    ):
        await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        listing = await service.manifest(None, NAME, "1.0.0")
        entries = [entry.model_dump() for entry in listing.files]
        for entry in entries:
            if entry["path"] == "docs/guide.md":
                entry["size"] += 1
        await tamper(db_session, "1.0.0", manifest=json.dumps(entries))

        with pytest.raises(StorageError):
            await service.read_file(None, NAME, "1.0.0", "docs/guide.md")

    @pytest.mark.parametrize("prefix", ["../", "pkg", "a/b/", "wrong//"])
    async def test_a_tampered_root_prefix_is_never_served_warm(
        self, db_session, service, docs, prefix
    ):
        await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        await tamper(db_session, "1.0.0", root_prefix=prefix)

        with pytest.raises(StorageError):
            await service.read_file(None, NAME, "1.0.0", "docs/guide.md")

    async def test_content_that_was_verified_survives_the_loss_of_its_blob(
        self, service, store, blob_store, docs
    ):
        warm = await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        await blob_store.delete(docs.version.sha256)

        again = await service.read_file(None, NAME, "1.0.0", "docs/guide.md")
        with pytest.raises(StorageError):
            await service.read_file(None, NAME, "1.0.0", "scripts/run.py")

        assert again == warm
        assert len(store.opened_keys) == 2


class TestSharedBytesShareTheCache:
    async def test_two_versions_with_the_same_bytes_are_read_from_the_blob_once(
        self, service, store, actors
    ):
        data = make_skill_zip(NAME, None, files=FILES)
        first = await service.upload(actors.owner, data, version="1.0.0", publish=True)
        second = await service.upload(actors.owner, data, version="1.0.1", publish=True)
        assert first.version.sha256 == second.version.sha256

        old = await service.entrypoint(None, NAME, "1.0.0")
        new = await service.entrypoint(None, NAME, "1.0.1")

        assert len(store.opened_keys) == 1
        assert (old.content, old.sha256) == (new.content, new.sha256)
        assert (old.version, new.version) == ("1.0.0", "1.0.1")

    async def test_different_packages_never_share_an_entry(self, service, store, actors):
        await service.upload(
            actors.owner, make_skill_zip("first-one", "1.0.0", body="# first\n"), publish=True
        )
        await service.upload(
            actors.owner, make_skill_zip("second-one", "1.0.0", body="# second\n"), publish=True
        )

        first = await service.entrypoint(None, "first-one", "1.0.0")
        second = await service.entrypoint(None, "second-one", "1.0.0")

        assert "# first" in first.content and "# second" not in first.content
        assert "# second" in second.content and "# first" not in second.content
        assert len(store.opened_keys) == 2


class TestTheBudgetHoldsInTheService:
    async def test_a_small_budget_keeps_only_the_most_recent_files(self, db_session, store, actors):
        cache = ContentCache(6_000)
        small = new_service(db_session, store, cache=cache)
        files = {
            f"docs/file-{number:02d}.md": ("x" * 1_500) + f"{number}\n" for number in range(12)
        }
        await small.upload(actors.owner, make_skill_zip(NAME, "1.0.0", files=files), publish=True)

        for number in range(12):
            await small.read_file(None, NAME, "1.0.0", f"docs/file-{number:02d}.md")
            assert cache.total_bytes <= 6_000
        opened = len(store.opened_keys)
        await small.read_file(None, NAME, "1.0.0", "docs/file-11.md")
        assert len(store.opened_keys) == opened
        await small.read_file(None, NAME, "1.0.0", "docs/file-00.md")

        assert len(store.opened_keys) == opened + 1
        assert cache.total_bytes <= 6_000

    async def test_a_file_bigger_than_the_whole_budget_is_read_every_time(
        self, db_session, store, actors
    ):
        cache = ContentCache(4_000)
        small = new_service(db_session, store, cache=cache)
        files = {"docs/big.md": "y" * 20_000}
        await small.upload(actors.owner, make_skill_zip(NAME, "1.0.0", files=files), publish=True)

        for _ in range(3):
            content = await small.read_file(None, NAME, "1.0.0", "docs/big.md")
            assert content.content == "y" * 20_000

        assert len(store.opened_keys) == 3
        assert cache.total_bytes <= 4_000

    async def test_a_budget_of_zero_turns_the_cache_off_without_changing_any_answer(
        self, db_session, store, actors
    ):
        off = new_service(db_session, store, cache=ContentCache(0))
        await off.upload(actors.owner, make_skill_zip(NAME, "1.0.0", files=FILES), publish=True)

        first = await off.entrypoint(None, NAME, "1.0.0")
        second = await off.entrypoint(None, NAME, "1.0.0")

        listing = await off.manifest(None, NAME, "1.0.0")

        assert first == second
        assert len(store.opened_keys) == 2
        assert len(listing.files) == len(FILES) + 1
        assert len(off.cache) == 0

    async def test_the_default_cache_never_outgrows_its_budget_with_many_packages(
        self, db_session, store, actors, monkeypatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 262144)
        service = new_service(db_session, store)
        service.cache.max_bytes = 50_000
        for number in range(40):
            name = f"bulk-{number:02d}"
            await service.upload(
                actors.owner,
                make_skill_zip(name, "1.0.0", body=("z" * 3_000) + f"{number}\n"),
                publish=True,
            )
            await service.entrypoint(None, name, "1.0.0")
            assert service.cache.total_bytes <= 50_000

        assert 0 < len(service.cache) < 80
