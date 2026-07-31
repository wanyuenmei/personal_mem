"""ScopeRegistry behavior against a real SQLite backend (temp file per test).

The Postgres path differs only in connection factory and paramstyle (both
covered by _sql/_connect), so these tests exercise the shared SQL and
semantics: slugging, upsert-only re-registration, per-user partitioning, and
the tenant guard.
"""

import pytest

from context_layer.consent import RESERVED_OWNER_SLUG, ScopeRegistry, slugify


@pytest.fixture
def registry(tmp_path):
    return ScopeRegistry(sqlite_path=str(tmp_path / "consent.db"))


def test_slugify_canonicalizes_names():
    assert slugify("Dietary Prefs!") == "dietary-prefs"
    assert slugify("  dietary-prefs ") == "dietary-prefs"
    assert slugify("TasteBuds") == "tastebuds"
    assert slugify("!!!") == ""
    assert len(slugify("x" * 100)) <= 40


def test_register_and_read_back(registry):
    scopes = registry.register(
        "mei",
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "food preferences and allergies"), ("location", "")],
    )

    assert [s.key for s in scopes] == ["dietary__tastebuds", "location__tastebuds"]
    assert scopes[0].owner_type == "third_party"
    assert scopes[0].owner_name == "tastebuds"
    assert scopes[0].description == "food preferences and allergies"
    assert registry.get("mei", "dietary__tastebuds") == scopes[0]
    assert registry.get("mei", "nope__tastebuds") is None


def test_reregistration_updates_without_duplicating(registry):
    registry.register(
        "mei",
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "old description")],
    )

    scopes = registry.register(
        "mei",
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "new description")],
    )

    assert len(scopes) == 1
    assert scopes[0].description == "new description"


def test_reregistration_never_deletes_missing_scopes(registry):
    """Upsert-only: a party that stops sending a scope doesn't retract it —
    a future grant may reference its key."""
    registry.register(
        "mei",
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", ""), ("location", "")],
    )

    scopes = registry.register(
        "mei",
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "still here")],
    )

    assert {s.key for s in scopes} == {"dietary__tastebuds", "location__tastebuds"}


def test_registry_is_per_user(registry):
    registry.register(
        "mei",
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "")],
    )

    assert registry.all("someone-else") == []
    assert registry.get("someone-else", "dietary__tastebuds") is None


def test_all_spans_owners_and_sorts(registry):
    registry.register(
        "mei",
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "")],
    )
    registry.register(
        "mei",
        owner_type="user",
        owner_slug=RESERVED_OWNER_SLUG,
        scopes=[("journaling", "private notes")],
    )

    scopes = registry.all("mei")

    assert [(s.owner_name, s.name) for s in scopes] == [
        ("tastebuds", "dietary"),
        ("user", "journaling"),
    ]


def test_delete_removes_one_scope(registry):
    registry.register(
        "mei",
        owner_type="user",
        owner_slug=RESERVED_OWNER_SLUG,
        scopes=[("journaling", ""), ("finances", "")],
    )

    assert registry.delete("mei", "journaling__user") is True

    assert registry.get("mei", "journaling__user") is None
    assert [s.key for s in registry.all("mei")] == ["finances__user"]


def test_delete_of_absent_key_reports_false(registry):
    assert registry.delete("mei", "nope__user") is False


def test_delete_is_per_user(registry):
    """One tenant deleting a key must never touch another tenant's row for
    the same key."""
    for user in ("mei", "someone-else"):
        registry.register(
            user,
            owner_type="user",
            owner_slug=RESERVED_OWNER_SLUG,
            scopes=[("journaling", "")],
        )

    assert registry.delete("someone-else", "journaling__user") is True

    assert registry.get("mei", "journaling__user") is not None


def test_delete_refuses_blank_user_id(registry):
    with pytest.raises(ValueError, match="tenant-isolation"):
        registry.delete("", "journaling__user")


def test_blank_user_id_is_refused(registry):
    with pytest.raises(ValueError, match="tenant-isolation"):
        registry.register(
            "", owner_type="user", owner_slug="user", scopes=[("a", "")]
        )
    with pytest.raises(ValueError, match="tenant-isolation"):
        registry.all("   ")
    with pytest.raises(ValueError, match="tenant-isolation"):
        registry.get(None, "a__user")


def test_unsluggable_scope_name_is_refused(registry):
    with pytest.raises(ValueError, match="no valid characters"):
        registry.register(
            "mei", owner_type="user", owner_slug="user", scopes=[("!!!", "")]
        )


def test_requires_exactly_one_backend(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        ScopeRegistry()
    with pytest.raises(ValueError, match="exactly one"):
        ScopeRegistry(
            sqlite_path=str(tmp_path / "x.db"), database_url="postgres://x"
        )
