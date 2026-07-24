"""Tests that search_memory/add_memory turn backend failures into friendly
errors instead of letting mem0/LLM/DB exceptions propagate to the MCP client.

context_layer.tools.memory_tools constructs a module-level ContextStore() on
import, which (unpatched) can try to download a fastembed model from Hugging
Face. The server_module fixture patches mem0.Memory.from_config out *before*
importing the module, so import is fully offline.
"""

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def server_module(monkeypatch):
    import mem0

    monkeypatch.setattr(mem0.Memory, "from_config", lambda cfg: MagicMock())

    sys.modules.pop("context_layer.tools.memory_tools", None)
    from context_layer.tools import memory_tools

    yield memory_tools

    sys.modules.pop("context_layer.tools.memory_tools", None)


def test_search_memory_returns_friendly_error_on_backend_failure(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module._store, "search", MagicMock(side_effect=RuntimeError("db down"))
    )

    result = server_module.search_memory("anything")

    assert "couldn't search" in result.lower()
    assert "RuntimeError" not in result
    assert "db down" not in result


def test_add_memory_returns_friendly_error_on_backend_failure(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module._store, "add", MagicMock(side_effect=RuntimeError("db down"))
    )

    result = server_module.add_memory("I like tea")

    assert "couldn't save" in result.lower()
    assert "RuntimeError" not in result
    assert "db down" not in result


def test_search_memory_wraps_any_exception_type(server_module, monkeypatch):
    """Not just RuntimeError — any mem0/LLM/DB exception should be caught."""

    class WeirdBackendError(Exception):
        pass

    monkeypatch.setattr(
        server_module._store, "search", MagicMock(side_effect=WeirdBackendError("boom"))
    )

    result = server_module.search_memory("anything")

    assert "couldn't search" in result.lower()


def test_search_memory_happy_path_still_works(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module._store,
        "search",
        MagicMock(
            return_value=[{"memory": "likes coffee", "metadata": {"scope": "general"}}]
        ),
    )

    result = server_module.search_memory("coffee")

    assert "likes coffee" in result


def test_add_memory_happy_path_still_works(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module._store, "add", MagicMock(return_value={"results": [{"id": "1"}]})
    )
    monkeypatch.setattr(server_module, "classify", lambda text: "dietary")

    result = server_module.add_memory("I like tea")

    assert "scope=dietary" in result


def test_add_memory_scope_comes_from_the_classifier(server_module, monkeypatch):
    """The caller can't set scope — the server classifies what it is saving."""
    add = MagicMock(return_value={"results": [{"id": "1"}]})
    monkeypatch.setattr(server_module._store, "add", add)
    monkeypatch.setattr(server_module, "classify", lambda text: "health")

    server_module.add_memory("I started running again")

    assert add.call_args.kwargs["scope"] == "health"


def test_add_memory_logs_the_tool_call(server_module, monkeypatch, caplog):
    monkeypatch.setattr(
        server_module._store, "add", MagicMock(return_value={"results": []})
    )

    with caplog.at_level("INFO", logger="context_layer.access"):
        server_module.add_memory("I like tea")

    [record] = [r for r in caplog.records if "tool_call" in r.getMessage()]
    message = record.getMessage()
    assert "tool=add_memory" in message
    assert "client=stdio" in message
    assert "ts=" in message


def test_search_memory_logs_scope_none(server_module, monkeypatch, caplog):
    monkeypatch.setattr(server_module._store, "search", MagicMock(return_value=[]))

    with caplog.at_level("INFO", logger="context_layer.access"):
        server_module.search_memory("anything")

    [record] = [r for r in caplog.records if "tool_call" in r.getMessage()]
    message = record.getMessage()
    assert "tool=search_memory" in message
    assert "scope=none" in message
