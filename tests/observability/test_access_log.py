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


def _request(user_agent: str = "", capability_label=None):
    """A request as the tool sees it: headers, plus the guard's state slot."""
    return SimpleNamespace(
        state=SimpleNamespace(client=capability_label),
        headers={"user-agent": user_agent} if user_agent else {},
    )


def _stateless_session():
    """A session as `stateless_http=True` produces it — no `initialize`, so no
    client_params. The shape production actually emits."""
    return SimpleNamespace(client_params=None)


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

    access_log.log_tool_call(
        "add_memory", _ctx(request=_request("claude-ai/1.0"), session=_stateless_session())
    )

    r = _record(logged)
    assert r["user"] == "workos_user_01ABC"
    assert r["client"] == "claude-ai/1.0"
    assert r["client_id"] == "client_01XYZ"


def test_capability_label_wins_over_self_asserted_user_agent(logged, monkeypatch):
    """A server-assigned label is trustworthy; a User-Agent is not — prefer it."""
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")
    request = _request("pretending-to-be-claude", capability_label="cursor")

    access_log.log_tool_call("search_memory", _ctx(request=request))

    assert _record(logged)["client"] == "cursor"


def test_stateless_session_still_yields_a_client(logged, monkeypatch):
    """Regression: `clientInfo` is unreachable under stateless_http, so a session
    with no client_params must still name the caller, from the User-Agent."""
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")

    access_log.log_tool_call(
        "search_memory", _ctx(request=_request("python-httpx/0.27"), session=_stateless_session())
    )

    assert _record(logged)["client"] == "python-httpx/0.27"


def test_missing_user_agent_falls_back_to_unlabeled(logged, monkeypatch):
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")

    access_log.log_tool_call(
        "search_memory", _ctx(request=_request(), session=_stateless_session())
    )

    assert _record(logged)["client"] == "unlabeled"


def test_client_name_cannot_forge_sibling_attributes(logged, monkeypatch):
    """A hostile User-Agent must stay one string value, not a second key."""
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "workos_real")
    hostile = _request('x", "user": "workos_victim')

    access_log.log_tool_call("search_memory", _ctx(request=hostile))

    r = _record(logged)
    assert r["user"] == "workos_real"
    assert r["client"] == 'x", "user": "workos_victim'


def test_long_user_agent_is_truncated(logged, monkeypatch):
    monkeypatch.setattr(access_log, "resolve_user_id", lambda ctx: "mei")

    access_log.log_tool_call("search_memory", _ctx(request=_request("x" * 500)))

    assert len(_record(logged)["client"]) == access_log._MAX_FIELD

