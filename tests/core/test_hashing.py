import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from usmp.core.hashing import canonical_json, content_hash, project, sha256_hex, usmp_id_for

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4)
    ),
    max_leaves=20,
)


def test_canonical_json_sorts_keys_and_strips_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, {"d": None, "c": True}]}) == (
        b'{"a":[1,{"c":true,"d":null}],"b":1}'
    )


def test_canonical_json_keeps_unicode_unescaped() -> None:
    assert canonical_json({"title": "Café ☕"}) == '{"title":"Café ☕"}'.encode()


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(bad: float) -> None:
    with pytest.raises(ValueError, match="not JSON compliant"):
        canonical_json({"x": bad})


@given(json_values)
def test_canonical_json_round_trips_and_is_stable(value: object) -> None:
    encoded = canonical_json(value)

    assert json.loads(encoded) == value
    assert canonical_json(json.loads(encoded)) == encoded


@given(st.dictionaries(st.text(), st.integers(), min_size=2))
def test_key_order_does_not_change_hash(payload: dict[str, int]) -> None:
    reordered = dict(reversed(list(payload.items())))

    assert content_hash(domain="d", type="t", schema_version=1, payload=payload) == content_hash(
        domain="d", type="t", schema_version=1, payload=reordered
    )


def test_hash_covers_domain_type_and_version() -> None:
    base = {"domain": "demo", "type": "event", "schema_version": 1, "payload": {"a": 1}}
    hashes = {
        content_hash(**base),  # type: ignore[arg-type]
        content_hash(**{**base, "domain": "contacts"}),  # type: ignore[arg-type]
        content_hash(**{**base, "type": "todo"}),  # type: ignore[arg-type]
        content_hash(**{**base, "schema_version": 2}),  # type: ignore[arg-type]
        content_hash(**{**base, "payload": {"a": 2}}),  # type: ignore[arg-type]
    }

    assert len(hashes) == 5


def test_sha256_hex_format() -> None:
    assert sha256_hex(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_project_keeps_only_present_requested_fields() -> None:
    assert project({"a": 1, "b": 2, "c": 3}, ["a", "c", "missing"]) == {"a": 1, "c": 3}


def test_usmp_id_is_deterministic_and_scoped_by_connector() -> None:
    first = usmp_id_for("google", "abc")

    assert first == usmp_id_for("google", "abc")
    assert first != usmp_id_for("caldav", "abc")
    assert first.startswith("usmp:")
