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
    POLICY_VERSION,
    REASON_KEY,
    SOURCE_KEY,
    SOURCE_LLM,
    SOURCE_USER,
    STATE_ARCHIVED,
    STATE_KEEP,
    STATE_KEY,
    RetentionHandler,
    TriageFailed,
    Verdict,
    retention_updates,
    sweep,
    triage_one,
)
from context_layer.curation.sweep import SWEPT_KEY
from context_layer.jobs import fingerprint_of

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


# --- judging one memory ---------------------------------------------------


def test_a_failed_triage_raises_rather_than_reading_as_keep(monkeypatch):
    """"Keep" is what a tidy store looks like, so a call that never answered
    must not be able to report it. Raising is what makes the worker count the
    memory as failed instead of silently leaving it kept."""
    monkeypatch.setattr(sweep, "triage", _raising_triage({"a"}))
    store = _FakeStore()

    with pytest.raises(TriageFailed):
        triage_one(store, "u1", {"id": "m1", "memory": "a"}, "fp1")

    assert store.updates == []


def test_a_failed_write_raises_too(stub_triage):
    """A write that lost a race with a delete is the same fact from the user's
    side as a failed call: this memory did not get judged."""
    stub_triage(_ARCHIVE)
    store = _FakeStore()

    def flaky(memory_id, updates, user_id=None):
        raise RuntimeError("write lost a race with a delete")

    store.update_metadata = flaky

    with pytest.raises(RuntimeError):
        triage_one(store, "u1", {"id": "m1", "memory": "a"}, "fp1")



# --- the pass, as the job worker sees it ----------------------------------
#
# The lifecycle around this — claiming, resuming, counting, the all-failed
# rule — is the worker's and is tested in tests/jobs. What is curation-specific
# is what the pass judges against and what judging one memory means.


def test_the_pass_reads_archived_memories_back_in(stub_triage):
    """This is the pass that can retract its own earlier verdict; skipping
    archived memories would make every archive permanent until a human
    intervened."""
    store = _FakeStore([
        {"id": "m1", "memory": "kept"},
        {"id": "m2", "memory": "aside", "metadata": {STATE_KEY: STATE_ARCHIVED}},
    ])

    assert len(RetentionHandler().prepare(store, "u1").rows) == 2


def test_what_the_pass_judges_against_is_the_policy_not_a_vocabulary():
    """Triage asks a fixed question, so bumping the policy version is how a
    changed prompt asks for the whole store to be re-judged."""
    handler = RetentionHandler()
    store = _FakeStore([])

    mine = handler.prepare(store, "u1").fingerprint
    assert mine == handler.prepare(store, "someone-else").fingerprint
    assert mine == fingerprint_of(POLICY_VERSION)


def test_a_memory_judged_under_the_current_policy_is_current():
    handler = RetentionHandler()

    assert handler.is_current({"metadata": {SWEPT_KEY: "fp"}}, "fp") is True
    assert handler.is_current({"metadata": {SWEPT_KEY: "old"}}, "fp") is False
    assert handler.is_current({"metadata": {}}, "fp") is False


def test_judging_a_memory_stamps_it_even_when_the_verdict_changes_nothing(
    stub_triage,
):
    """A keep verdict on an unarchived memory moves nothing — but it has been
    judged, and the stamp is what stops the next pass paying to ask again."""
    stub_triage(_KEEP)
    store = _FakeStore([])

    counted = triage_one(store, "u1", {"id": "m1", "memory": "x"}, "fp1")

    assert counted == {}
    assert store.updates == [("m1", {SWEPT_KEY: "fp1"}, "u1")]


def test_setting_a_memory_aside_counts_both_ways(stub_triage):
    """``changed`` is what every kind of pass reports; the archived/restored
    split is what this one owes the user."""
    stub_triage(_ARCHIVE)
    store = _FakeStore([])

    counted = triage_one(store, "u1", {"id": "m1", "memory": "x"}, "fp1")

    assert counted == {"changed": 1, "archived": 1}
    assert store.updates[0][1][STATE_KEY] == STATE_ARCHIVED


def test_putting_one_back_counts_as_restored(stub_triage):
    stub_triage(_KEEP)
    store = _FakeStore([])
    row = {"id": "m1", "memory": "x", "metadata": {STATE_KEY: STATE_ARCHIVED}}

    counted = triage_one(store, "u1", row, "fp1")

    assert counted == {"changed": 1, "restored": 1}


def test_a_row_without_text_or_id_is_not_judged_at_all(stub_triage):
    store = _FakeStore([])

    assert triage_one(store, "u1", {"id": "m1", "memory": ""}, "fp1") == {}
    assert triage_one(store, "u1", {"id": "", "memory": "x"}, "fp1") == {}
    assert store.updates == []
