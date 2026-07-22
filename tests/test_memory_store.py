"""Tests for ContextStore (memory.py): scope filtering + basic shape.

These exercise ContextStore's own logic — how it builds filters and
normalizes mem0's response shape — against a mocked mem0.Memory (see
fake_mem0 in conftest.py), not against a real vector store.
"""

import pytest

from context_layer.memory import ContextStore, TenantIsolationError


def test_search_without_scope_filters_only_by_user(fake_mem0):
    fake_mem0.search.return_value = {"results": [{"memory": "likes coffee"}]}
    store = ContextStore()

    results = store.search("coffee", user_id="alice", limit=3)

    fake_mem0.search.assert_called_once_with(
        "coffee", filters={"user_id": "alice"}, top_k=3
    )
    assert results == [{"memory": "likes coffee"}]


def test_search_with_scope_pushes_scope_into_filters(fake_mem0):
    fake_mem0.search.return_value = {"results": []}
    store = ContextStore()

    store.search("coffee", user_id="alice", limit=5, scope="shopping")

    fake_mem0.search.assert_called_once_with(
        "coffee", filters={"user_id": "alice", "scope": "shopping"}, top_k=5
    )


def test_search_scope_none_is_not_present_in_filters(fake_mem0):
    """scope=None (the default) must NOT add a "scope" key at all — a caller
    should get results across every scope, not be filtered to scope=None."""
    fake_mem0.search.return_value = []
    store = ContextStore()

    store.search("coffee", user_id="alice")

    _, kwargs = fake_mem0.search.call_args
    assert "scope" not in kwargs["filters"]


def test_search_without_user_id_raises(fake_mem0):
    """PER-5 hardening: a falsy user_id must raise (no silent default), so a
    resolver bug can't search with no per-tenant filter."""
    store = ContextStore()

    with pytest.raises(TenantIsolationError):
        store.search("coffee")


def test_search_normalizes_bare_list_response(fake_mem0):
    fake_mem0.search.return_value = [{"memory": "x"}]
    store = ContextStore()

    assert store.search("q", user_id="alice") == [{"memory": "x"}]


def test_search_normalizes_non_list_non_dict_response(fake_mem0):
    fake_mem0.search.return_value = None
    store = ContextStore()

    assert store.search("q", user_id="alice") == []


def test_add_stores_scope_in_metadata(fake_mem0):
    fake_mem0.add.return_value = {"results": [{"id": "1"}]}
    store = ContextStore()

    store.add("I like tea", user_id="bob", scope="dietary")

    fake_mem0.add.assert_called_once()
    args, kwargs = fake_mem0.add.call_args
    assert args[0] == "I like tea"
    assert kwargs["user_id"] == "bob"
    assert kwargs["metadata"] == {"scope": "dietary"}


def test_add_defaults_scope_to_general(fake_mem0):
    fake_mem0.add.return_value = {"results": []}
    store = ContextStore()

    store.add("some fact", user_id="bob")

    _, kwargs = fake_mem0.add.call_args
    assert kwargs["metadata"] == {"scope": "general"}


def test_add_without_user_id_raises(fake_mem0):
    """PER-5 hardening: add must refuse a falsy user_id rather than defaulting."""
    store = ContextStore()

    with pytest.raises(TenantIsolationError):
        store.add("some fact")


def test_all_filters_by_user_id_only(fake_mem0):
    fake_mem0.get_all.return_value = {"results": [{"memory": "x"}]}
    store = ContextStore()

    results = store.all(user_id="alice")

    fake_mem0.get_all.assert_called_once_with(filters={"user_id": "alice"})
    assert results == [{"memory": "x"}]
