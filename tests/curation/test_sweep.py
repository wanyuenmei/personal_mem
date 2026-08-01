"""Where a triage verdict becomes a retention state: the reconcile rule and
the user-triggered pass.

Triage itself is stubbed everywhere here (its own behaviour is
tests/curation/test_triage.py), so these tests are about provenance,
restraint and threading: what gets written, what is deliberately left alone,
and what a pass reports when it could not do its job.
"""

import time

import pytest

from context_layer.curation import (
    REASON_KEY,
    SOURCE_KEY,
    SOURCE_LLM,
    SOURCE_USER,
    STATE_ARCHIVED,
    STATE_KEEP,
    STATE_KEY,
    TriageFailed,
    Verdict,
    retention_updates,
    sweep,
    triage_rows,
)
from context_layer.curation.sweep import (
    TRIAGE_ERROR_ALL_FAILED,
    TriageRunner,
)

_ARCHIVE = Verdict(state=STATE_ARCHIVED, reason="one-off task detail")
_KEEP = Verdict(state=STATE_KEEP)


class _FakeStore:
    """Records update_metadata calls and serves rows to the pass."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.updates = []

    def all(self, user_id, limit=1000):
        return self.rows

    def update_metadata(self, memory_id, updates, user_id=None):
        self.updates.append((memory_id, updates, user_id))
        return {"updated": True, "id": memory_id}


@pytest.fixture
def stub_triage(monkeypatch):
    """Pin what triage "decides" for any text."""

    def install(verdict):
        monkeypatch.setattr(sweep, "triage", lambda text: verdict)

    return install


def _raising_triage(fails_on, verdict=_ARCHIVE):
    """Triage that fails for the given memory texts and decides `verdict` for
    the rest — how a bad API key or a rate limit looks from triage_rows."""

    def triage(text):
        if text in fails_on:
            raise TriageFailed("the triage call failed")
        return verdict

    return triage


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- the reconcile rule ---------------------------------------------------


def test_an_archive_verdict_writes_the_state_its_source_and_the_reason():
    assert retention_updates({}, _ARCHIVE) == {
        STATE_KEY: STATE_ARCHIVED,
        SOURCE_KEY: SOURCE_LLM,
        REASON_KEY: "one-off task detail",
    }


def test_a_keep_verdict_on_an_untouched_memory_writes_nothing():
    """Keep is the state of every memory with no retention keys at all, so
    recording it would mean a write — and a re-embed — per memory to store the
    default."""
    assert retention_updates({}, _KEEP) == {}


def test_a_keep_verdict_retracts_an_earlier_archive():
    metadata = {STATE_KEY: STATE_ARCHIVED, SOURCE_KEY: SOURCE_LLM, REASON_KEY: "trivia"}

    assert retention_updates(metadata, _KEEP) == {
        STATE_KEY: STATE_KEEP,
        SOURCE_KEY: SOURCE_LLM,
        REASON_KEY: "",
    }


def test_re_archiving_an_archived_memory_writes_nothing():
    """A second pass over a settled store must not rewrite every row it still
    agrees with."""
    metadata = {STATE_KEY: STATE_ARCHIVED, SOURCE_KEY: SOURCE_LLM, REASON_KEY: "trivia"}

    assert retention_updates(metadata, _ARCHIVE) == {}


def test_a_memory_the_user_kept_is_never_archived_by_a_pass():
    """The escape hatch that makes an automatic pass safe to run: what the
    user decided is intent, not derivation."""
    metadata = {STATE_KEY: STATE_KEEP, SOURCE_KEY: SOURCE_USER, REASON_KEY: ""}

    assert retention_updates(metadata, _ARCHIVE) == {}


def test_a_memory_the_user_set_aside_is_never_put_back_by_a_pass():
    """The same rule in the other direction, which is the one that would
    otherwise resurrect something the user chose to hide."""
    metadata = {STATE_KEY: STATE_ARCHIVED, SOURCE_KEY: SOURCE_USER, REASON_KEY: ""}

    assert retention_updates(metadata, _KEEP) == {}


# --- triage_rows ----------------------------------------------------------


def test_triage_rows_writes_through_the_tenant_guarded_primitive(stub_triage):
    stub_triage(_ARCHIVE)
    store = _FakeStore()

    counts = triage_rows(store, "u1", [{"id": "m1", "memory": "the file is at /tmp/x"}])

    assert (counts.archived, counts.restored, counts.failed) == (1, 0, 0)
    memory_id, updates, user_id = store.updates[0]
    assert (memory_id, user_id) == ("m1", "u1")
    assert updates[STATE_KEY] == STATE_ARCHIVED


def test_archived_and_restored_are_counted_apart(stub_triage):
    """A pass that set forty memories aside and one that put forty back are
    the same size and opposite events."""
    stub_triage(_KEEP)
    store = _FakeStore()

    counts = triage_rows(
        store, "u1",
        [
            {"id": "m1", "memory": "a", "metadata": {STATE_KEY: STATE_ARCHIVED}},
            {"id": "m2", "memory": "b"},
        ],
    )

    assert (counts.archived, counts.restored, counts.failed) == (0, 1, 0)
    assert [call[0] for call in store.updates] == ["m1"]


def test_rows_without_text_or_id_are_skipped(stub_triage):
    stub_triage(_ARCHIVE)
    store = _FakeStore()

    counts = triage_rows(
        store, "u1", [{"id": "m1", "memory": ""}, {"id": "", "memory": "x"}]
    )

    assert (counts.archived, counts.failed) == (0, 0)
    assert store.updates == []


def test_a_failed_triage_is_counted_not_read_as_keep(monkeypatch):
    """"Keep" is what a tidy store looks like, so a call that never answered
    must not be able to report it."""
    monkeypatch.setattr(sweep, "triage", _raising_triage({"a"}))
    store = _FakeStore()

    counts = triage_rows(store, "u1", [{"id": "m1", "memory": "a"}])

    assert (counts.archived, counts.restored, counts.failed) == (0, 0, 1)
    assert store.updates == []


def test_one_memorys_failure_does_not_abandon_the_rest(monkeypatch):
    monkeypatch.setattr(sweep, "triage", _raising_triage({"a"}))
    store = _FakeStore()

    counts = triage_rows(
        store, "u1", [{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}]
    )

    assert (counts.archived, counts.failed) == (1, 1)
    assert [call[0] for call in store.updates] == ["m2"]


def test_a_failed_write_is_counted_too(stub_triage):
    stub_triage(_ARCHIVE)
    store = _FakeStore()
    original = store.update_metadata

    def flaky(memory_id, updates, user_id=None):
        if memory_id == "m1":
            raise RuntimeError("write lost a race with a delete")
        return original(memory_id, updates, user_id)

    store.update_metadata = flaky

    counts = triage_rows(
        store, "u1", [{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}]
    )

    assert (counts.archived, counts.failed) == (1, 1)


# --- the user-triggered pass ----------------------------------------------


def test_a_pass_walks_every_memory_and_reports_done(stub_triage):
    stub_triage(_ARCHIVE)
    store = _FakeStore([{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}])
    runner = TriageRunner()

    assert runner.start(store, "u1") is True
    assert _wait_for(lambda: runner.status("u1").state == "done")

    status = runner.status("u1")
    assert (status.total, status.processed, status.archived) == (2, 2, 2)
    assert status.finished_at
    assert {call[0] for call in store.updates} == {"m1", "m2"}


def test_a_pass_reads_archived_memories_back_in(stub_triage):
    """Skipping them would make every archive permanent until a human
    intervened — the pass has to be able to retract its own verdict."""
    stub_triage(_KEEP)
    store = _FakeStore(
        [{"id": "m1", "memory": "a", "metadata": {STATE_KEY: STATE_ARCHIVED}}]
    )
    runner = TriageRunner()

    runner.start(store, "u1")
    assert _wait_for(lambda: runner.status("u1").state == "done")

    assert runner.status("u1").restored == 1


def test_a_second_pass_while_one_runs_is_refused(monkeypatch):
    """One click, one pass: an impatient user must not be able to multiply the
    API calls a pass over the whole store costs."""
    release = {"go": False}
    monkeypatch.setattr(
        sweep, "triage",
        lambda text: (_wait_for(lambda: release["go"], 5.0), _KEEP)[1],
    )
    store = _FakeStore([{"id": "m1", "memory": "a"}])
    runner = TriageRunner()

    assert runner.start(store, "u1") is True
    assert _wait_for(lambda: runner.is_running("u1"))
    assert runner.start(store, "u1") is False

    release["go"] = True
    assert _wait_for(lambda: runner.status("u1").state == "done")


def test_pass_status_is_per_user(stub_triage):
    stub_triage(_KEEP)
    store = _FakeStore([{"id": "m1", "memory": "a"}])
    runner = TriageRunner()

    runner.start(store, "u1")
    assert _wait_for(lambda: runner.status("u1").state == "done")

    assert runner.status("u2").state == "idle"


def test_a_pass_that_could_review_nothing_does_not_report_success(monkeypatch):
    """Every memory failing is a broken triage call, not a store that was
    already exactly right — the two used to look identical."""
    monkeypatch.setattr(sweep, "triage", _raising_triage({"a", "b"}))
    store = _FakeStore([{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}])
    runner = TriageRunner()

    runner.start(store, "u1")
    assert _wait_for(lambda: runner.status("u1").state == "error")

    status = runner.status("u1")
    assert status.error == TRIAGE_ERROR_ALL_FAILED
    assert (status.total, status.archived, status.failed) == (2, 0, 2)
    assert status.finished_at


def test_a_partly_failing_pass_still_reports_what_landed(monkeypatch):
    monkeypatch.setattr(sweep, "triage", _raising_triage({"a"}))
    store = _FakeStore([{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}])
    runner = TriageRunner()

    runner.start(store, "u1")
    assert _wait_for(lambda: runner.status("u1").state == "done")

    status = runner.status("u1")
    assert (status.processed, status.archived, status.failed) == (2, 1, 1)
    assert status.error == ""


def test_a_failing_pass_reports_error_not_a_stuck_running():
    class _Broken:
        def all(self, user_id, limit=1000):
            raise RuntimeError("backend down")

    runner = TriageRunner()
    runner.start(_Broken(), "u1")

    assert _wait_for(lambda: runner.status("u1").state == "error")
    assert runner.status("u1").error == "RuntimeError"
