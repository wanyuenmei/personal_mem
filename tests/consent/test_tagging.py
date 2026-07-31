"""Where classifier output becomes tags: the reconcile rule, the full sweep,
and the on-write pass.

The classifier itself is stubbed everywhere here (its own behaviour is
tests/consent/test_classifier.py) so these tests are about provenance and
threading: what gets written, what is left alone, and what must never be
allowed to fail a write.
"""

import time

import pytest

from context_layer.consent import (
    PROVENANCE_LLM,
    PROVENANCE_LLM_CLEARED,
    PROVENANCE_USER,
    PROVENANCE_USER_REMOVED,
    ConsentScope,
    ScopeRegistry,
    tag_updates,
    tagging,
)
from context_layer.consent.tagging import SweepRunner, tag_new_memories, tag_rows

_DIETARY = ConsentScope(
    key="dietary__tastebuds",
    owner_type="third_party",
    owner_name="tastebuds",
    name="dietary",
    description="food",
)
_TRAVEL = ConsentScope(
    key="travel__user",
    owner_type="user",
    owner_name="user",
    name="travel",
    description="trips",
)
_SCOPES = [_DIETARY, _TRAVEL]


class _FakeStore:
    """Records update_metadata calls and serves rows to the sweep."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.updates = []

    def all(self, user_id, limit=1000):
        return self.rows

    def update_metadata(self, memory_id, updates, user_id=None):
        self.updates.append((memory_id, updates, user_id))
        return {"updated": True, "id": memory_id}


@pytest.fixture
def stub_classify(monkeypatch):
    """Pin what the classifier "decides" for any text."""

    def install(keys):
        monkeypatch.setattr(tagging, "classify", lambda text, scopes: list(keys))

    return install


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- the reconcile rule ---------------------------------------------------


def test_a_picked_scope_is_tagged_llm():
    assert tag_updates({}, _SCOPES, ["travel__user"]) == {
        "cs_travel__user": PROVENANCE_LLM
    }


def test_a_manual_tag_survives_the_classifier():
    """A `user` tag is intent, not derivation: a sweep never rewrites it, even
    when the classifier agrees with it."""
    metadata = {"cs_travel__user": PROVENANCE_USER}

    assert tag_updates(metadata, _SCOPES, ["travel__user"]) == {}
    assert tag_updates(metadata, _SCOPES, []) == {}


def test_a_user_removed_tombstone_is_never_re_applied():
    """The whole point of the tombstone: the classifier vetoed once stays
    vetoed, however many sweeps run."""
    metadata = {"cs_dietary__tastebuds": PROVENANCE_USER_REMOVED}

    assert tag_updates(metadata, _SCOPES, ["dietary__tastebuds"]) == {}


def test_a_stale_llm_tag_is_retracted_not_deleted():
    """mem0 can't remove a metadata key, so an llm tag that no longer applies
    is written down to llm_cleared, which reads as untagged."""
    metadata = {"cs_travel__user": PROVENANCE_LLM}

    assert tag_updates(metadata, _SCOPES, []) == {
        "cs_travel__user": PROVENANCE_LLM_CLEARED
    }


def test_a_cleared_tag_can_come_back():
    """Unlike user_removed, llm_cleared carries no user intent — a later sweep
    may set it again."""
    metadata = {"cs_travel__user": PROVENANCE_LLM_CLEARED}

    assert tag_updates(metadata, _SCOPES, ["travel__user"]) == {
        "cs_travel__user": PROVENANCE_LLM
    }


def test_scopes_never_tagged_are_not_written_at_all():
    """Otherwise every memory would grow a key per registered scope."""
    assert tag_updates({}, _SCOPES, []) == {}


def test_an_unchanged_memory_produces_no_write():
    """A re-sweep of a settled store must not re-embed every memory."""
    metadata = {"cs_travel__user": PROVENANCE_LLM}

    assert tag_updates(metadata, _SCOPES, ["travel__user"]) == {}


# --- tag_rows -------------------------------------------------------------


def test_tag_rows_writes_through_the_tenant_guarded_primitive(stub_classify):
    stub_classify(["travel__user"])
    store = _FakeStore()

    changed = tag_rows(store, "u1", _SCOPES, [{"id": "m1", "memory": "went to Rome"}])

    assert changed == 1
    assert store.updates == [("m1", {"cs_travel__user": PROVENANCE_LLM}, "u1")]


def test_one_memorys_failure_does_not_abandon_the_rest(stub_classify, caplog):
    stub_classify(["travel__user"])
    store = _FakeStore()
    failed = {"done": False}
    original = store.update_metadata

    def flaky(memory_id, updates, user_id=None):
        if memory_id == "m1" and not failed["done"]:
            failed["done"] = True
            raise RuntimeError("write lost a race with a delete")
        return original(memory_id, updates, user_id)

    store.update_metadata = flaky

    changed = tag_rows(
        store, "u1", _SCOPES,
        [{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}],
    )

    assert changed == 1
    assert [call[0] for call in store.updates] == ["m2"]


def test_rows_without_text_or_id_are_skipped(stub_classify):
    stub_classify(["travel__user"])
    store = _FakeStore()

    changed = tag_rows(
        store, "u1", _SCOPES, [{"id": "m1", "memory": ""}, {"id": "", "memory": "x"}]
    )

    assert changed == 0
    assert store.updates == []


# --- the user-triggered sweep --------------------------------------------


@pytest.fixture
def registry(tmp_path):
    reg = ScopeRegistry(sqlite_path=str(tmp_path / "consent.db"))
    reg.register(
        "u1", owner_type="user", owner_slug="user", scopes=[("travel", "trips")]
    )
    return reg


def test_sweep_tags_every_memory_and_reports_done(registry, stub_classify):
    stub_classify([registry.all("u1")[0].key])
    store = _FakeStore([{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}])
    runner = SweepRunner()

    assert runner.start(store, registry, "u1") is True
    assert _wait_for(lambda: runner.status("u1").state == "done")

    status = runner.status("u1")
    assert (status.total, status.processed, status.changed) == (2, 2, 2)
    assert status.finished_at
    assert {call[0] for call in store.updates} == {"m1", "m2"}


def test_a_second_sweep_while_one_runs_is_refused(registry, monkeypatch):
    """One click, one pass: an impatient user must not be able to multiply the
    API calls a sweep costs."""
    release = {"go": False}
    monkeypatch.setattr(
        tagging, "classify",
        lambda text, scopes: (_wait_for(lambda: release["go"], 5.0), [])[1],
    )
    store = _FakeStore([{"id": "m1", "memory": "a"}])
    runner = SweepRunner()

    assert runner.start(store, registry, "u1") is True
    assert _wait_for(lambda: runner.is_running("u1"))
    assert runner.start(store, registry, "u1") is False

    release["go"] = True
    assert _wait_for(lambda: runner.status("u1").state == "done")


def test_sweep_status_is_per_user(registry, stub_classify):
    stub_classify([])
    store = _FakeStore([{"id": "m1", "memory": "a"}])
    runner = SweepRunner()

    runner.start(store, registry, "u1")
    assert _wait_for(lambda: runner.status("u1").state == "done")

    assert runner.status("u2").state == "idle"


def test_a_sweep_with_no_registered_scopes_reads_no_memories(stub_classify, tmp_path):
    """Nothing to classify into means the store is never even listed."""
    stub_classify([])
    empty = ScopeRegistry(sqlite_path=str(tmp_path / "empty.db"))
    store = _FakeStore([{"id": "m1", "memory": "a"}])
    runner = SweepRunner()

    runner.start(store, empty, "u1")
    assert _wait_for(lambda: runner.status("u1").state == "done")

    assert runner.status("u1").total == 0
    assert store.updates == []


def test_a_failing_sweep_reports_error_not_a_stuck_running(registry, monkeypatch):
    class _Broken:
        def all(self, user_id, limit=1000):
            raise RuntimeError("backend down")

    runner = SweepRunner()
    runner.start(_Broken(), registry, "u1")

    assert _wait_for(lambda: runner.status("u1").state == "error")
    assert runner.status("u1").error == "RuntimeError"


# --- the on-write pass ----------------------------------------------------


def test_on_write_tagging_classifies_only_the_new_ids(
    registry, stub_classify, monkeypatch
):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    key = registry.all("u1")[0].key
    stub_classify([key])
    store = _FakeStore(
        [{"id": "m1", "memory": "new one"}, {"id": "m0", "memory": "already there"}]
    )

    thread = tag_new_memories(
        store, "u1", {"results": [{"id": "m1", "event": "ADD"}]}, registry=registry
    )
    assert thread is not None
    thread.join(timeout=5)

    assert [call[0] for call in store.updates] == ["m1"]


def test_on_write_tagging_reads_metadata_rather_than_trusting_the_add_result(
    registry, stub_classify, monkeypatch
):
    """An UPDATE event lands on a memory that may already carry a user tag the
    pass must not clobber — which is only visible by re-reading the row."""
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    key = registry.all("u1")[0].key
    stub_classify([key])
    store = _FakeStore(
        [{"id": "m1", "memory": "x", "metadata": {f"cs_{key}": PROVENANCE_USER_REMOVED}}]
    )

    thread = tag_new_memories(
        store, "u1", {"results": [{"id": "m1", "event": "UPDATE"}]}, registry=registry
    )
    assert thread is not None
    thread.join(timeout=5)

    assert store.updates == []


def test_on_write_tagging_is_skipped_when_the_classifier_is_off(
    registry, monkeypatch
):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: False)
    store = _FakeStore([{"id": "m1", "memory": "x"}])

    assert (
        tag_new_memories(
            store, "u1", {"results": [{"id": "m1", "event": "ADD"}]}, registry=registry
        )
        is None
    )
    assert store.updates == []


def test_deleted_and_noop_events_are_not_classified(registry, monkeypatch):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    store = _FakeStore([{"id": "m1", "memory": "x"}])

    result = {"results": [{"id": "m1", "event": "DELETE"}, {"id": "m2", "event": "NONE"}]}

    assert tag_new_memories(store, "u1", result, registry=registry) is None
    assert store.updates == []


def test_on_write_tagging_never_raises_into_the_write_path(registry, monkeypatch):
    """add_memory has already saved the user's memory by this point; a
    classifier problem must not turn that into a failed tool call."""
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)

    class _Broken:
        def all(self, user_id, limit=1000):
            raise RuntimeError("backend down")

    thread = tag_new_memories(
        _Broken(), "u1", {"results": [{"id": "m1", "event": "ADD"}]}, registry=registry
    )
    if thread is not None:
        thread.join(timeout=5)


def test_a_malformed_add_result_is_survivable(registry, monkeypatch):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    store = _FakeStore()

    assert tag_new_memories(store, "u1", "not a result", registry=registry) is None
    assert tag_new_memories(store, "u1", {"results": None}, registry=registry) is None
