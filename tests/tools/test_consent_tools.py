"""register_scopes: validation messages, friendly backend errors, and what
actually lands in the registry.

The tool is called directly as a function (ctx=None resolves to the
single-tenant default user), with the module's lazy registry pointed at a
temp SQLite file.
"""

import json
from unittest.mock import MagicMock

import pytest

from context_layer.consent import ScopeRegistry
from context_layer.tools import consent_tools


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = ScopeRegistry(sqlite_path=str(tmp_path / "consent.db"))
    monkeypatch.setattr(consent_tools, "_registry", reg)
    return reg


def test_register_scopes_happy_path(registry):
    result = consent_tools.register_scopes(
        "TasteBuds",
        [
            {"name": "dietary", "description": "food preferences and allergies"},
            {"name": "location"},
        ],
    )

    assert "'tastebuds'" in result
    assert "2 scope(s)" in result
    assert "dietary — food preferences and allergies" in result
    keys = {s.key for s in registry.all("mei")}
    assert keys == {"dietary__tastebuds", "location__tastebuds"}


def test_register_scopes_is_idempotent(registry):
    consent_tools.register_scopes("tastebuds", [{"name": "dietary"}])
    result = consent_tools.register_scopes(
        "tastebuds", [{"name": "dietary", "description": "updated"}]
    )

    assert "1 scope(s)" in result
    [scope] = registry.all("mei")
    assert scope.description == "updated"


def test_register_scopes_rejects_reserved_party(registry):
    result = consent_tools.register_scopes("User", [{"name": "dietary"}])

    assert "reserved" in result
    assert registry.all("mei") == []


def test_register_scopes_rejects_unsluggable_party(registry):
    result = consent_tools.register_scopes("!!!", [{"name": "dietary"}])

    assert "Could not register" in result
    assert registry.all("mei") == []


def test_register_scopes_rejects_empty_scope_list(registry):
    result = consent_tools.register_scopes("tastebuds", [])

    assert "at least one scope" in result


def test_register_scopes_rejects_too_many_scopes(registry):
    scopes: list[consent_tools.ScopeSpec] = [
        {"name": f"scope-{i}"} for i in range(21)
    ]

    result = consent_tools.register_scopes("tastebuds", scopes)

    assert "at most 20" in result
    assert registry.all("mei") == []


def test_register_scopes_rejects_blank_scope_name(registry):
    result = consent_tools.register_scopes("tastebuds", [{"name": "  "}])

    assert "Could not register" in result
    assert registry.all("mei") == []


def test_register_scopes_rejects_oversized_description(registry):
    result = consent_tools.register_scopes(
        "tastebuds", [{"name": "dietary", "description": "x" * 301}]
    )

    assert "too long" in result
    assert registry.all("mei") == []


def test_register_scopes_rejects_overlong_party(registry):
    result = consent_tools.register_scopes("p" * 41, [{"name": "dietary"}])

    assert "too long" in result
    assert registry.all("mei") == []


def test_register_scopes_rejects_overlong_scope_name(registry):
    result = consent_tools.register_scopes("tastebuds", [{"name": "n" * 41}])

    assert "too long" in result
    assert registry.all("mei") == []


def test_register_scopes_rejects_names_that_collide_after_slugging(registry):
    result = consent_tools.register_scopes(
        "tastebuds",
        [
            {"name": "Dietary Prefs", "description": "kept out"},
            {"name": "dietary  prefs!", "description": "colliding"},
        ],
    )

    assert "same scope once normalized" in result
    assert registry.all("mei") == []


def test_register_scopes_returns_friendly_error_on_backend_failure(
    registry, monkeypatch
):
    monkeypatch.setattr(
        registry, "register", MagicMock(side_effect=RuntimeError("db down"))
    )

    result = consent_tools.register_scopes("tastebuds", [{"name": "dietary"}])

    assert "couldn't save" in result.lower()
    assert "RuntimeError" not in result
    assert "db down" not in result


def test_register_scopes_logs_the_tool_call(registry, caplog):
    with caplog.at_level("INFO", logger="context_layer.access"):
        consent_tools.register_scopes("tastebuds", [{"name": "dietary"}])

    [record] = [r for r in caplog.records if "tool_call" in r.getMessage()]
    logged = json.loads(record.getMessage())
    assert logged["tool"] == "register_scopes"


def test_get_registry_builds_sqlite_registry_by_default(monkeypatch):
    monkeypatch.setattr(consent_tools, "_registry", None)

    registry = consent_tools.get_registry()

    assert isinstance(registry, ScopeRegistry)
    assert consent_tools.get_registry() is registry
