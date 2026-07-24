"""Tests for the batch backfill runner (ingest/runner.py).

Pure logic against a fake store — no mem0, no LLM, no network.
"""

import pytest

from context_layer.ingest.normalized import Conversation, Message
from context_layer.ingest.runner import estimate, run_backfill
from context_layer.memory import TenantIsolationError


class FakeStore:
    """Records add() calls and returns a fixed 2-fact result (or raises for a
    chosen source_id, to exercise the failure path)."""

    def __init__(self, fail_on=None):
        self.calls = []
        self._fail_on = fail_on

    def add(self, content, user_id, extra_metadata=None, infer=None):
        self.calls.append(
            {
                "content": content,
                "user_id": user_id,
                "extra_metadata": extra_metadata,
                "infer": infer,
            }
        )
        if extra_metadata and extra_metadata.get("source_id") == self._fail_on:
            raise RuntimeError("boom")
        return {"results": [{"id": "a"}, {"id": "b"}]}


def _conv(cid, title, msgs):
    return Conversation(
        source="claude",
        source_id=cid,
        title=title,
        messages=[Message(role=r, text=t) for r, t in msgs],
    )


def _fixture():
    return [
        _conv("c1", "Coffee", [("user", "I like dark roast"), ("assistant", "noted")]),
        _conv("c2", "Travel", [("user", "trip to Kyoto")]),
    ]


def test_run_backfill_adds_per_conversation_with_provenance():
    store = FakeStore()

    result = run_backfill(_fixture(), store, "mei")

    assert result.conversations == 2
    assert result.facts_added == 4  # 2 conversations × 2 facts each
    assert result.failures == 0
    # messages converted to role/content dicts mem0 accepts
    assert store.calls[0]["content"] == [
        {"role": "user", "content": "I like dark roast"},
        {"role": "assistant", "content": "noted"},
    ]
    assert store.calls[0]["extra_metadata"] == {"source": "claude", "source_id": "c1"}


def test_infer_override_is_forwarded_to_store():
    """The extractor choice (0-cost vs LLM) is passed straight through to
    store.add as `infer`."""
    store = FakeStore()

    run_backfill(_fixture(), store, "mei", infer=False)

    assert [c["infer"] for c in store.calls] == [False, False]


def test_infer_defaults_to_none_following_config():
    store = FakeStore()

    run_backfill(_fixture(), store, "mei")

    assert [c["infer"] for c in store.calls] == [None, None]


def test_limit_caps_conversations():
    store = FakeStore()

    result = run_backfill(_fixture(), store, "mei", limit=1)

    assert result.conversations == 1
    assert len(store.calls) == 1


def test_empty_conversation_is_skipped():
    convs = [_conv("c1", "empty", []), _conv("c2", "ok", [("user", "hi")])]
    store = FakeStore()

    result = run_backfill(convs, store, "mei")

    assert result.skipped == 1
    assert len(store.calls) == 1


def test_failure_is_counted_and_does_not_abort():
    store = FakeStore(fail_on="c1")

    result = run_backfill(_fixture(), store, "mei")

    assert result.failures == 1
    assert result.facts_added == 2  # c2 still imported after c1 failed
    assert len(store.calls) == 2  # both attempted


def test_tenant_isolation_error_propagates_not_swallowed():
    """A blank/invalid user_id is a whole-run config error: run_backfill must
    let TenantIsolationError fail fast, never count it as a per-item failure and
    keep importing the rest of the batch under a bad tenant."""

    class _RaisingStore:
        def add(self, *args, **kwargs):
            raise TenantIsolationError("blank user_id")

    with pytest.raises(TenantIsolationError):
        run_backfill(_fixture(), _RaisingStore(), "")


def test_progress_callback_receives_per_conversation_rows():
    store = FakeStore()
    rows = []

    run_backfill(
        _fixture(),
        store,
        "mei",
        on_progress=lambda i, total, conv, n: rows.append((i, total, conv.source_id, n)),
    )

    assert rows == [(1, 2, "c1", 2), (2, 2, "c2", 2)]


def test_estimate_counts_messages_and_calls():
    est = estimate(_fixture())

    assert est.conversations == 2
    assert est.messages == 3
    assert est.llm_calls == 2  # one extraction call per conversation
    assert est.approx_input_tokens >= 0


def test_estimate_zero_cost_extractor_has_no_calls_or_cost():
    est = estimate(_fixture(), with_extraction=False)

    assert est.llm_calls == 0
    assert est.approx_cost_usd == 0.0


def test_estimate_respects_limit_and_ignores_empty_for_calls():
    convs = [_conv("c1", "empty", []), _conv("c2", "ok", [("user", "hi there")])]

    est = estimate(convs)

    assert est.conversations == 2
    assert est.messages == 1
    assert est.llm_calls == 1  # only the non-empty conversation costs a call
