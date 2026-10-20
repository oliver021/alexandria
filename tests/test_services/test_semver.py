import itertools
import random
from functools import cmp_to_key

import pytest

from app.services import semver
from app.services.errors import DomainError, ValidationError
from app.services.semver import InvalidVersionError, Version


@pytest.fixture(autouse=True)
def setup_db():
    yield


PRECEDENCE_CHAIN = [
    "0.0.0",
    "0.0.1",
    "0.1.0",
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    "1.0.0",
    "1.0.1",
    "1.2.3",
    "1.10.0",
    "2.0.0-0",
    "2.0.0",
    "10.0.0",
]

VALID_VERSIONS = [
    ("0.0.0", (0, 0, 0, ())),
    ("1.2.3", (1, 2, 3, ())),
    ("10.20.30", (10, 20, 30, ())),
    ("1.0.0-alpha", (1, 0, 0, ("alpha",))),
    ("1.0.0-alpha.1", (1, 0, 0, ("alpha", "1"))),
    ("1.0.0-0.3.7", (1, 0, 0, ("0", "3", "7"))),
    ("1.0.0-x.7.z.92", (1, 0, 0, ("x", "7", "z", "92"))),
    ("1.0.0-x-y-z.--", (1, 0, 0, ("x-y-z", "--"))),
    ("1.0.0--", (1, 0, 0, ("-",))),
    ("1.0.0-0A", (1, 0, 0, ("0A",))),
    ("1.0.0-a.0", (1, 0, 0, ("a", "0"))),
    ("1.0.0-RC.1", (1, 0, 0, ("RC", "1"))),
    ("1.0.0-0", (1, 0, 0, ("0",))),
    ("99999999999.0.0", (99999999999, 0, 0, ())),
]

INVALID_VERSIONS = [
    "",
    " ",
    "1",
    "1.0",
    "1.0.0.0",
    "v1.0.0",
    "V1.0.0",
    "=1.0.0",
    "^1.0.0",
    "~1.0.0",
    "1.x.0",
    "1.*.0",
    "latest",
    "01.0.0",
    "1.01.0",
    "1.0.01",
    "-1.0.0",
    "1.-1.0",
    "1.0.-1",
    "1.0.0-",
    "1.0.0-.",
    "1.0.0-a.",
    "1.0.0-.a",
    "1.0.0-01",
    "1.0.0-a.01",
    "1.0.0-alpha..1",
    "1.0.0-alpha_beta",
    "1.0.0-alpha beta",
    "1.0.0+build",
    "1.0.0+build.5",
    "1.0.0-alpha+build",
    "1.0.0+",
    " 1.0.0",
    "1.0.0 ",
    "1.0.0\n",
    "\n1.0.0",
    "1.0.0\t",
    "1. 0.0",
    "1.0.0\x00",
    "1,0,0",
    "1.0.0-" + chr(0xE9),
    "".join(chr(code) for code in (0x661, 0x2E, 0x660, 0x2E, 0x660)),
    "1.0.0-" + chr(0x661),
    "1.0.0-alpha.1.",
    "..",
    "1..0",
]


class TestParse:
    @pytest.mark.parametrize(("text", "expected"), VALID_VERSIONS)
    def test_valid_versions_are_parsed_into_their_fields(self, text, expected):
        major, minor, patch, prerelease = expected
        version = semver.parse(text)
        assert (version.major, version.minor, version.patch, version.prerelease) == expected
        assert version.is_prerelease is bool(prerelease)
        assert str(version) == text
        assert semver.is_valid(text) is True
        assert (major, minor, patch) == (version.major, version.minor, version.patch)

    @pytest.mark.parametrize("text", INVALID_VERSIONS)
    def test_invalid_versions_are_rejected(self, text):
        with pytest.raises(InvalidVersionError):
            semver.parse(text)
        assert semver.is_valid(text) is False

    @pytest.mark.parametrize(
        "value", [None, 1, 1.0, b"1.0.0", ["1.0.0"], ("1", "0", "0"), object()]
    )
    def test_non_string_values_are_rejected(self, value):
        with pytest.raises(InvalidVersionError, match="string"):
            semver.parse(value)
        assert semver.is_valid(value) is False

    def test_build_metadata_is_rejected_with_a_specific_message(self):
        with pytest.raises(InvalidVersionError, match="build metadata"):
            semver.parse("1.0.0+20240101")

    def test_trailing_newline_is_not_tolerated(self):
        assert semver.is_valid("1.0.0") is True
        assert semver.is_valid("1.0.0\n") is False

    def test_non_ascii_digits_are_not_digits(self):
        arabic_one = chr(0x661)
        assert arabic_one.isdigit()
        assert semver.is_valid(f"{arabic_one}.0.0") is False
        assert semver.is_valid(f"1.0.0-{arabic_one}") is False

    def test_length_limit_is_fifty_characters(self):
        assert semver.MAX_VERSION_LENGTH == 50
        at_limit = "1.0.0-" + "a" * 44
        over_limit = "1.0.0-" + "a" * 45
        assert len(at_limit) == 50
        assert semver.is_valid(at_limit) is True
        assert semver.is_valid(over_limit) is False
        with pytest.raises(InvalidVersionError, match="longer"):
            semver.parse(over_limit)

    def test_huge_input_fails_fast_without_integer_conversion_errors(self):
        with pytest.raises(InvalidVersionError):
            semver.parse("9" * 10_000 + ".0.0")

    def test_error_message_quotes_a_truncated_value(self):
        with pytest.raises(InvalidVersionError) as caught:
            semver.parse("x" * 40)
        assert "xxxx" in str(caught.value)
        long_value = "v" + "1" * 49
        with pytest.raises(InvalidVersionError) as caught:
            semver.parse(long_value + "z")
        assert len(str(caught.value)) < 200


class TestInvalidVersionError:
    def test_is_a_validation_domain_error_and_a_value_error(self):
        with pytest.raises(InvalidVersionError) as caught:
            semver.parse("nope")
        error = caught.value
        assert isinstance(error, ValidationError)
        assert isinstance(error, DomainError)
        assert isinstance(error, ValueError)
        assert error.code == "VALIDATION_ERROR"
        assert error.message == str(error)
        assert "nope" in error.message

    def test_can_be_caught_as_the_generic_value_error(self):
        with pytest.raises(ValueError):
            semver.parse("")


class TestPrecedence:
    def test_the_official_chain_is_strictly_increasing(self):
        versions = [semver.parse(text) for text in PRECEDENCE_CHAIN]
        for lower, higher in itertools.combinations(versions, 2):
            assert lower < higher
            assert higher > lower
            assert lower <= higher
            assert higher >= lower
            assert lower != higher
            assert not higher < lower
            assert not lower > higher

    def test_compare_returns_minus_one_zero_or_one(self):
        for lower, higher in itertools.combinations(PRECEDENCE_CHAIN, 2):
            assert semver.compare(lower, higher) == -1
            assert semver.compare(higher, lower) == 1
        for text in PRECEDENCE_CHAIN:
            assert semver.compare(text, text) == 0

    def test_numbers_compare_numerically_not_lexicographically(self):
        assert semver.parse("1.10.0") > semver.parse("1.9.0")
        assert semver.parse("1.0.10") > semver.parse("1.0.9")
        assert semver.parse("10.0.0") > semver.parse("9.99.99")
        assert semver.parse("1.0.0-alpha.10") > semver.parse("1.0.0-alpha.9")
        assert semver.parse("1.0.0-beta.11") > semver.parse("1.0.0-beta.2")

    def test_huge_numeric_identifiers_do_not_overflow(self):
        assert semver.parse("1.0.0-99999999999999999999") > semver.parse("1.0.0-9")
        assert semver.parse("1.0.0-99999999999999999998") < semver.parse(
            "1.0.0-99999999999999999999"
        )

    def test_numeric_identifiers_have_lower_precedence_than_alphanumeric_ones(self):
        assert semver.parse("1.0.0-1") < semver.parse("1.0.0-a")
        assert semver.parse("1.0.0-999999") < semver.parse("1.0.0-0a")
        assert semver.parse("1.0.0-alpha.1") < semver.parse("1.0.0-alpha.beta")
        assert semver.parse("1.0.0-alpha.999") < semver.parse("1.0.0-alpha.a")

    def test_alphanumeric_identifiers_follow_ascii_order(self):
        assert semver.parse("1.0.0-A") < semver.parse("1.0.0-a")
        assert semver.parse("1.0.0-Z") < semver.parse("1.0.0-a")
        assert semver.parse("1.0.0-a") < semver.parse("1.0.0-b")
        assert semver.parse("1.0.0-a") < semver.parse("1.0.0-a-b")
        assert semver.parse("1.0.0-a-1") < semver.parse("1.0.0-a0")
        assert semver.parse("1.0.0-rc") < semver.parse("1.0.0-rc-1")

    def test_a_larger_set_of_prerelease_fields_has_higher_precedence(self):
        assert semver.parse("1.0.0-alpha") < semver.parse("1.0.0-alpha.1")
        assert semver.parse("1.0.0-alpha.1") < semver.parse("1.0.0-alpha.1.1")
        assert semver.parse("1.0.0-beta.2") < semver.parse("1.0.0-beta.2.0")

    def test_a_release_outranks_any_prerelease_of_the_same_core(self):
        assert semver.parse("1.0.0-zzz.999") < semver.parse("1.0.0")
        assert semver.parse("2.0.0-rc.1") > semver.parse("1.9.9")
        assert semver.parse("2.0.0-rc.1") < semver.parse("2.0.0")

    def test_the_core_dominates_the_prerelease(self):
        assert semver.parse("1.0.1-alpha") > semver.parse("1.0.0")
        assert semver.parse("1.1.0-alpha") > semver.parse("1.0.9")
        assert semver.parse("2.0.0-alpha") > semver.parse("1.99.99")

    def test_equal_versions_are_equal_and_hash_alike(self):
        first = semver.parse("1.2.3-rc.1")
        second = semver.parse("1.2.3-rc.1")
        assert first == second
        assert hash(first) == hash(second)
        assert len({first, second}) == 1
        assert first <= second
        assert first >= second
        assert not first < second

    def test_versions_are_immutable(self):
        version = semver.parse("1.2.3")
        with pytest.raises(AttributeError):
            version.major = 9

    def test_comparing_with_other_types_is_not_supported(self):
        version = semver.parse("1.0.0")
        assert version != "1.0.0"
        with pytest.raises(TypeError):
            _ = version < "1.0.0"

    def test_order_is_a_consistent_total_order_on_random_versions(self):
        rng = random.Random(2024)
        identifiers = ["0", "1", "2", "10", "alpha", "beta", "A", "a-b", "0a", "rc"]

        def random_version() -> str:
            core = f"{rng.randint(0, 3)}.{rng.randint(0, 3)}.{rng.randint(0, 3)}"
            if rng.random() < 0.5:
                return core
            parts = [rng.choice(identifiers) for _ in range(rng.randint(1, 3))]
            return core + "-" + ".".join(parts)

        versions = list({random_version() for _ in range(120)})
        by_key = sorted(versions, key=semver.sort_key)
        by_compare = sorted(versions, key=cmp_to_key(semver.compare))
        assert by_key == by_compare
        for first, second, third in itertools.islice(itertools.permutations(by_key, 3), 0, 4000, 7):
            if semver.compare(first, second) <= 0 and semver.compare(second, third) <= 0:
                assert semver.compare(first, third) <= 0
            assert semver.compare(first, second) == -semver.compare(second, first)


class TestSorting:
    def test_sort_versions_orders_a_shuffled_chain(self):
        for seed in range(5):
            shuffled = PRECEDENCE_CHAIN[:]
            random.Random(seed).shuffle(shuffled)
            assert semver.sort_versions(shuffled) == PRECEDENCE_CHAIN

    def test_sort_versions_can_return_descending_order(self):
        assert semver.sort_versions(PRECEDENCE_CHAIN, reverse=True) == PRECEDENCE_CHAIN[::-1]

    def test_sort_versions_accepts_any_iterable_and_returns_a_new_list(self):
        original = ("1.10.0", "1.2.0", "1.9.0")
        result = semver.sort_versions(iter(original))
        assert result == ["1.2.0", "1.9.0", "1.10.0"]
        assert isinstance(result, list)

    def test_sort_versions_of_nothing_is_empty(self):
        assert semver.sort_versions([]) == []

    def test_sort_versions_rejects_an_invalid_version(self):
        with pytest.raises(InvalidVersionError):
            semver.sort_versions(["1.0.0", "banana"])

    def test_sort_key_orders_strings_like_versions(self):
        assert semver.sort_key("1.0.0-rc.1") < semver.sort_key("1.0.0")
        assert semver.sort_key("1.0.0") < semver.sort_key("1.0.1")


class TestPickLatest:
    def test_nothing_published_has_no_latest(self):
        assert semver.pick_latest([]) is None
        assert semver.pick_latest(iter(())) is None

    def test_a_single_version_is_the_latest(self):
        assert semver.pick_latest(["3.2.1"]) == "3.2.1"
        assert semver.pick_latest(["3.2.1-rc.1"]) == "3.2.1-rc.1"

    def test_the_highest_stable_version_wins(self):
        assert semver.pick_latest(["1.0.0", "1.10.0", "1.9.0", "0.9.9"]) == "1.10.0"

    def test_stable_versions_are_preferred_over_a_higher_prerelease(self):
        assert semver.pick_latest(["1.0.0", "2.0.0-rc.1"]) == "1.0.0"
        assert semver.pick_latest(["2.0.0-rc.1", "1.0.0", "1.5.0", "3.0.0-alpha"]) == "1.5.0"

    def test_when_only_prereleases_exist_the_highest_one_wins(self):
        assert semver.pick_latest(["1.0.0-alpha", "1.0.0-beta", "1.0.0-alpha.1"]) == "1.0.0-beta"
        assert semver.pick_latest(["2.0.0-rc.1", "2.0.0-rc.2", "1.9.0-rc.9"]) == "2.0.0-rc.2"

    def test_a_stable_release_displaces_its_own_prereleases(self):
        assert semver.pick_latest(["1.0.0-rc.1", "1.0.0"]) == "1.0.0"

    def test_the_result_does_not_depend_on_input_order(self):
        versions = ["1.0.0", "1.2.0-rc.1", "1.1.0", "0.5.0", "1.1.0-beta"]
        for permutation in itertools.permutations(versions):
            assert semver.pick_latest(permutation) == "1.1.0"

    def test_duplicates_are_harmless(self):
        assert semver.pick_latest(["1.0.0", "1.0.0", "0.1.0"]) == "1.0.0"

    def test_the_original_string_is_returned(self):
        result = semver.pick_latest(["1.0.0", "2.0.0"])
        assert result == "2.0.0"
        assert isinstance(result, str)

    def test_an_invalid_version_is_not_silently_ignored(self):
        with pytest.raises(InvalidVersionError):
            semver.pick_latest(["1.0.0", "banana"])


class TestVersionObject:
    def test_str_round_trips_through_parse(self):
        for text in PRECEDENCE_CHAIN:
            assert str(semver.parse(text)) == text

    def test_a_version_can_be_built_directly(self):
        assert Version(1, 2, 3) == semver.parse("1.2.3")
        assert Version(1, 2, 3, ("rc", "1")) == semver.parse("1.2.3-rc.1")
        assert str(Version(0, 0, 1, ("alpha",))) == "0.0.1-alpha"
