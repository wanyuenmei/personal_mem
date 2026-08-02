"""The persisted run: what may be enqueued, claimed, and resumed.

A real RunStore over temp SQLite — the rows are the thing under test, so a
stub of them would test nothing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from context_layer.jobs import (
    STATE_DONE,
    STATE_QUEUED,
    STATE_RUNNING,
    RunStore,
    SweepRun,
    fingerprint_of,
)

KIND = "scope_tagging"
USER = "mei"


@pytest.fixture
def runs(tmp_path):
    return RunStore(sqlite_path=str(tmp_path / "consent.db"))


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_a_user_with_no_history_has_no_run(runs):
    assert runs.get(USER, KIND) is None


def test_enqueueing_records_a_queued_run(runs):
    assert runs.enqueue(USER, KIND) is True

    run = runs.get(USER, KIND)
    assert run.state == STATE_QUEUED
    assert run.queued_at
    # Not known yet: what a pass judges against is settled by the worker that
    # runs it, so a queued run has nothing to say about it.
    assert run.fingerprint == ""


def test_a_second_ask_while_one_is_pending_is_refused(runs):
    """The button is one click; an impatient user must not be able to queue a
    second full pass and double the API calls."""
    runs.enqueue(USER, KIND)

    assert runs.enqueue(USER, KIND) is False


def test_asking_again_after_one_finished_starts_a_fresh_run(runs):
    runs.enqueue(USER, KIND)
    runs.finish(USER, KIND, state=STATE_DONE)

    assert runs.enqueue(USER, KIND) is True
    assert runs.get(USER, KIND).state == STATE_QUEUED


def test_runs_are_per_user_and_per_kind(runs):
    runs.enqueue(USER, KIND)

    assert runs.enqueue("someone-else", KIND) is True
    assert runs.enqueue(USER, "retention") is True


# --- claiming and resuming -------------------------------------------------


def test_a_queued_run_is_claimable(runs):
    runs.enqueue(USER, KIND)

    [candidate] = runs.claimable()

    assert candidate.state == STATE_QUEUED
    claimed = runs.claim(candidate)
    assert claimed is not None
    assert claimed.state == STATE_RUNNING


def test_a_live_run_is_not_claimable(runs):
    """A worker that is merely busy must never be stolen from."""
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])

    assert runs.claimable() == []


def test_a_run_whose_worker_died_becomes_claimable_again(runs):
    """The deploy case: the process holding this run was killed, so nothing
    will ever beat its heart again. That is what a resume looks like."""
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])
    # Reach past the API to age the heartbeat — there is no way to wait five
    # minutes in a test, and the staleness rule is the thing being checked.
    with runs._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE sweep_runs SET heartbeat_at = ?", (_ago(30),)
        )
        conn.commit()

    [candidate] = runs.claimable()
    assert candidate.state == STATE_RUNNING
    assert runs.claim(candidate) is not None


def test_re_asking_for_a_stranded_run_is_allowed(runs):
    """A stranded run is not "already running" from the user's side — pressing
    the button again is exactly how they say "carry on with that"."""
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])
    with runs._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE sweep_runs SET heartbeat_at = ?", (_ago(30),))
        conn.commit()

    assert runs.enqueue(USER, KIND) is True


def test_two_workers_cannot_claim_the_same_run(runs):
    """The guard is the state and heartbeat the worker last saw, so the loser
    of the race gets None rather than a second copy of the same pass."""
    runs.enqueue(USER, KIND)
    candidate = runs.claimable()[0]

    first = runs.claim(candidate)
    second = runs.claim(candidate)

    assert first is not None
    assert second is None


def test_claiming_resets_the_counters(runs):
    """A resumed run re-walks from the top, skipping what is already current,
    so its counters describe THIS attempt rather than the dead one's."""
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])
    runs.progress(USER, KIND, processed=40, changed=7)
    with runs._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE sweep_runs SET heartbeat_at = ?", (_ago(30),))
        conn.commit()

    resumed = runs.claim(runs.claimable()[0])

    assert (resumed.processed, resumed.changed) == (0, 0)


# --- progress and finishing ------------------------------------------------


def test_progress_publishes_counters_and_beats_the_heart(runs):
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])
    before = runs.get(USER, KIND).heartbeat_at

    runs.progress(USER, KIND, total=9, processed=3, skipped=1, changed=2)

    run = runs.get(USER, KIND)
    assert (run.total, run.processed, run.skipped, run.changed) == (9, 3, 1, 2)
    assert run.heartbeat_at >= before


def test_per_kind_counters_survive_a_round_trip(runs):
    """Each pass counts its own things; the shared row carries them as JSON so
    adding a sweep kind is not a migration."""
    runs.enqueue(USER, "retention")
    runs.claim(runs.claimable()[0])

    runs.progress(USER, "retention", detail={"archived": 12, "restored": 3})

    assert runs.get(USER, "retention").detail == {"archived": 12, "restored": 3}


def test_the_page_shape_flattens_per_kind_counters_in(runs):
    """The page reads scope_count and archived like any other field rather
    than reaching a level down for them."""
    run = SweepRun(
        user_id=USER, kind=KIND, state=STATE_DONE, total=5, changed=2,
        detail={"scope_count": 3},
    )

    assert run.as_dict()["scope_count"] == 3
    assert run.as_dict()["changed"] == 2


def test_finishing_is_terminal_and_stamped(runs):
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])

    runs.finish(USER, KIND, state=STATE_DONE)

    run = runs.get(USER, KIND)
    assert run.state == STATE_DONE
    assert run.finished
    assert run.finished_at
    assert runs.claimable() == []


def test_a_run_with_an_unreadable_heartbeat_is_treated_as_abandoned(runs):
    """Recoverable beats crashing: a heartbeat nobody can parse should make a
    run resumable, not take out the worker trying to resume it."""
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])
    with runs._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE sweep_runs SET heartbeat_at = 'not-a-timestamp'")
        conn.commit()

    assert len(runs.claimable()) == 1


# --- fingerprints ----------------------------------------------------------


def test_the_same_inputs_hash_the_same_across_calls():
    """Stable across processes and restarts, or a resume would re-do the store."""
    assert fingerprint_of("a", "b") == fingerprint_of("a", "b")


def test_different_inputs_hash_differently():
    assert fingerprint_of("a", "b") != fingerprint_of("a", "c")


def test_a_fingerprint_is_short_enough_to_stamp_on_every_memory():
    assert len(fingerprint_of("anything")) == 16


def test_a_re_ask_cannot_clobber_a_run_a_worker_just_claimed(runs):
    """enqueue and claim race precisely on an abandoned run — both are allowed
    to take it. Without a compare-and-swap the re-ask would stamp the row back
    to queued underneath the worker, and a second worker could then claim a
    pass already being executed.
    """
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])
    with runs._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE sweep_runs SET heartbeat_at = ?", (_ago(30),))
        conn.commit()
    stale = runs.get(USER, KIND)  # what a re-ask would have read

    # A worker claims it first; the re-ask is deciding against a row that no
    # longer exists in that shape.
    assert runs.claim(stale) is not None
    reasked = runs.enqueue(USER, KIND)

    assert reasked is False
    assert runs.get(USER, KIND).state == STATE_RUNNING


def test_the_fingerprint_records_what_the_attempt_actually_used(runs):
    """Written when the pass starts rather than when it was asked for, so a
    stored result can be traced back to the vocabulary that produced it."""
    runs.enqueue(USER, KIND)
    runs.claim(runs.claimable()[0])

    runs.progress(USER, KIND, fingerprint="abc123", total=4)

    assert runs.get(USER, KIND).fingerprint == "abc123"
