"""Access-log tests: the JSON shape, the two identity fields, and forgery.

The log line is the audit trail *and* the query surface — Railway only promotes
keys to filterable attributes if the record is single-line JSON. So these tests
assert the record parses as JSON, that `user`/`client_id` say something
trustworthy, and that a caller-supplied client name can't forge either.
"""

import json
import logging
from types import SimpleNamespace
from typing import cast

import pytest
from mcp.server.fastmcp import Context

from context_layer.observability import access_log


class _FakeRequestContext:
    def __init__(self, request=None, session=None):
        self.request = request
        self.session = session


class _FakeContext:
    """Stands in for fastmcp's Context — only request_context is read."""

    def __init__(self, request=None, session=None):
        self._rc = _FakeRequestContext(request, session)

    @property
    def request_context(self):
        return self._rc


def _ctx(request=None, session=None) -> Context:
    """A stand-in Context. Cast because only `request_context` is exercised."""
    return cast(Context, _FakeContext(request, session))


def _session_named(name: str):
    return SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name=name)))


def _record(caplog) -> dict:
    """The emitted line, parsed back from JSON."""
    return json.loads(caplog.records[-1].getMessage())


@pytest.fixture
def logged(caplog):
    caplog.set_level(logging.INFO, logger="context_layer.access")
    return caplog


def test_record_is_single_line_json_with_reserved_keys(logged, monkeypatch):
    """Railway needs one-line JSON carrying `message`; anything else is opaque."""
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")

    access_log.log_tool_call("search_memory", None)

    raw = logged.records[-1].getMessage()
    assert "\n" not in raw
    r = json.loads(raw)
    assert r["message"] == "tool_call"
    assert r["level"] == "info"


def test_logs_default_user_and_stdio_client_without_context(logged, monkeypatch):
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")

    access_log.log_tool_call("search_memory", None)

    r = _record(logged)
    assert r["tool"] == "search_memory"
    assert r["user"] == "mei"
    assert r["client"] == "stdio"
    assert r["client_id"] == "none"


def test_logs_resolved_tenant_and_oauth_client_id(logged, monkeypatch):
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "workos_user_01ABC")
    monkeypatch.setattr(
        access_log, "get_access_token", lambda: SimpleNamespace(client_id="client_01XYZ")
    )

    access_log.log_tool_call("add_memory", _ctx(session=_session_named("claude-ai")))

    r = _record(logged)
    assert r["user"] == "workos_user_01ABC"
    assert r["client"] == "claude-ai"
    assert r["client_id"] == "client_01XYZ"


def test_capability_label_wins_over_self_asserted_client_name(logged, monkeypatch):
    """A server-assigned label is trustworthy; clientInfo is not — prefer it."""
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")
    request = SimpleNamespace(state=SimpleNamespace(client="cursor"))

    access_log.log_tool_call(
        "search_memory", _ctx(request=request, session=_session_named("pretending-to-be-claude"))
    )

    assert _record(logged)["client"] == "cursor"


def test_client_name_cannot_forge_sibling_attributes(logged, monkeypatch):
    """A hostile clientInfo.name must stay one string value, not a second key."""
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "workos_real")
    hostile = _session_named('x", "user": "workos_victim')

    access_log.log_tool_call("search_memory", _ctx(session=hostile))

    r = _record(logged)
    assert r["user"] == "workos_real"
    assert r["client"] == 'x", "user": "workos_victim'


def test_long_client_name_is_truncated(logged, monkeypatch):
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")

    access_log.log_tool_call("search_memory", _ctx(session=_session_named("x" * 500)))

    assert len(_record(logged)["client"]) == access_log._MAX_FIELD

