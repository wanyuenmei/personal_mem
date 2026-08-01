"""The worker: what it claims, what it skips, and what happens when it dies.

Driven a tick at a time rather than through its thread, so every assertion is
about the lifecycle rather than about timing. The handler is a stub — what a
row means is the feature's business and is tested there; what is under test
here is the machinery around it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from context_layer.jobs import (
    ERROR_ALL_FAILED,
    STATE_DONE,
    STATE_ERROR,
    STATE_RUNNING,
    Pass,
    RunStore,
    SweepWorker,
)

USER = "mei"


@pytest.fixture
def runs(tmp_path):
    return RunStore(sqlite_path=str(tmp_path / "consent.db"))


class _Handler:
    """A pass over rows the test supplies, with the outcomes it dictates."""

    kind = "demo"

    def __init__(self, rows, *, fingerprint="fp1", outcomes=None, current=()):
        self._rows = rows
        self._fingerprint = fingerprint
        # memory id -> what handle() does: a counter dict, or an Exception.
        self._outcomes = outcomes or {}
        self._current = set(current)
        self.handled = []

    def prepare(self, store, user_id):
        return Pass(
            fingerprint=self._fingerprint,
            rows=self._rows,
            detail={"widgets": 0},
        )

    def is_current(self, row, fingerprint):
        return row["id"] in self._current

    def handle(self, store, user_id, row, plan):
        self.handled.append(row["id"])
        outcome = self._outcomes.get(row["id"], {"changed": 1})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _rows(n):
    return [{"id": f"m{i}", "memory": f"fact {i}"} for i in range(n)]


def _age_heartbeat(runs, minutes=30):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with runs._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE sweep_runs SET heartbeat_at = ?", (stamp,))
        conn.commit()


def test_a_tick_with_nothing_queued_does_nothing(runs):
    worker = SweepWorker(runs, object(), [_Handler(_rows(3))])

    assert worker.tick() is False


def test_a_queued_run_is_executed_and_finishes(runs):
    handler = _Handler(_rows(3))
    worker = SweepWorker(runs, object(), [handler])
    runs.enqueue(USER, "demo")

    assert worker.tick() is True

    run = runs.get(USER, "demo")
    assert run.state == STATE_DONE
    assert (run.total, run.processed, run.changed) == (3, 3, 3)
    assert handler.handled == ["m0", "m1", "m2"]


def test_memories_already_current_are_skipped_without_being_handled(runs):
    """The whole point of the stamp: a repeat pass over an unchanged store
    should cost no model calls at all."""
    handler = _Handler(_rows(4), current=("m0", "m1", "m2", "m3"))
    worker = SweepWorker(runs, object(), [handler])
    runs.enqueue(USER, "demo")

    worker.tick()

    run = runs.get(USER, "demo")
    assert handler.handled == []
    assert (run.processed, run.skipped, run.changed) == (4, 4, 0)
    assert run.state == STATE_DONE


def test_a_pass_that_skipped_everything_is_a_success_not_a_failure(runs):
    """Nothing to do is the correct outcome for an unchanged store, and must
    not read like the broken-classifier case."""
    worker = SweepWorker(runs, object(), [_Handler(_rows(3), current=("m0", "m1", "m2"))])
    runs.enqueue(USER, "demo")

    worker.tick()

    run = runs.get(USER, "demo")
    assert (run.state, run.error) == (STATE_DONE, "")


def test_per_kind_counters_are_accumulated_and_published(runs):
    handler = _Handler(
        _rows(3),
        outcomes={"m0": {"widgets": 1}, "m1": {"widgets": 2}, "m2": {"changed": 1}},
    )
    worker = SweepWorker(runs, object(), [handler])
    runs.enqueue(USER, "demo")

    worker.tick()

    run = runs.get(USER, "demo")
    assert run.detail == {"widgets": 3}
    assert run.changed == 1


def test_one_rows_failure_does_not_abandon_the_rest(runs):
    handler = _Handler(_rows(3), outcomes={"m1": RuntimeError("upstream")})
    worker = SweepWorker(runs, object(), [handler])
    runs.enqueue(USER, "demo")

    worker.tick()

    run = runs.get(USER, "demo")
    assert (run.processed, run.changed, run.failed) == (3, 2, 1)
    assert run.state == STATE_DONE


def test_every_attempted_row_failing_is_an_error_not_a_clean_pass(runs):
    """The VC-91 rule, kept: a pass where every call was rejected must not
    finish as "done, 0 updated" over a store that needed work."""
    handler = _Handler(
        _rows(2), outcomes={"m0": RuntimeError("x"), "m1": RuntimeError("x")}
    )
    worker = SweepWorker(runs, object(), [handler])
    runs.enqueue(USER, "demo")

    worker.tick()

    run = runs.get(USER, "demo")
    assert (run.state, run.error) == (STATE_ERROR, ERROR_ALL_FAILED)


def test_only_attempted_rows_count_toward_all_failed(runs):
    """A pass that skipped most of the store and failed the one row it tried
    is still a broken pass — the skips must not dilute it into a success."""
    handler = _Handler(
        _rows(3), current=("m0", "m1"), outcomes={"m2": RuntimeError("x")}
    )
    worker = SweepWorker(runs, object(), [handler])
    runs.enqueue(USER, "demo")

    worker.tick()

    assert runs.get(USER, "demo").error == ERROR_ALL_FAILED


def test_a_handler_that_cannot_start_fails_the_run_rather_than_hanging_it(runs):
    class _Broken(_Handler):
        def prepare(self, store, user_id):
            raise RuntimeError("store is down")

    worker = SweepWorker(runs, object(), [_Broken(_rows(1))])
    runs.enqueue(USER, "demo")

    worker.tick()

    run = runs.get(USER, "demo")
    assert (run.state, run.error) == (STATE_ERROR, "RuntimeError")


def test_a_run_of_an_unknown_kind_is_left_alone(runs):
    """An older or newer deploy's run. Leaving it queued is recoverable;
    failing it would throw away work the right process could still do."""
    worker = SweepWorker(runs, object(), [_Handler(_rows(1))])
    runs.enqueue(USER, "something-else")

    assert worker.tick() is False
    assert runs.get(USER, "something-else").state != STATE_ERROR


# --- surviving the process that started it ---------------------------------


def test_a_run_stranded_by_a_dead_worker_is_resumed_by_the_next_one(runs):
    """The deploy case end to end: a run left running by a killed process is
    picked up and carried to done by the worker that boots after it."""
    runs.enqueue(USER, "demo")
    dead = SweepWorker(runs, object(), [_Handler(_rows(5))])
    runs.claim(runs.claimable()[0])  # a worker claimed it, then the box went away
    _age_heartbeat(runs)
    assert runs.get(USER, "demo").state == STATE_RUNNING

    fresh_handler = _Handler(_rows(5))
    fresh = SweepWorker(runs, object(), [fresh_handler])
    assert fresh.tick() is True

    run = runs.get(USER, "demo")
    assert run.state == STATE_DONE
    assert run.processed == 5
    assert dead is not None  # the first worker never ran a tick; nothing to join


def test_a_resume_pays_only_for_what_the_dead_worker_had_not_reached(runs):
    """Resuming re-walks from the top, but everything the first pass already
    stamped is skipped — so the second attempt costs the remainder, not the
    store."""
    runs.enqueue(USER, "demo")
    runs.claim(runs.claimable()[0])
    _age_heartbeat(runs)

    # The dead worker got through the first three before it was killed.
    handler = _Handler(_rows(5), current=("m0", "m1", "m2"))
    SweepWorker(runs, object(), [handler]).tick()

    assert handler.handled == ["m3", "m4"]
    run = runs.get(USER, "demo")
    assert (run.skipped, run.processed) == (3, 5)


def test_stopping_mid_pass_leaves_the_run_resumable(runs):
    """Shutdown must not finish a run it did not finish: it stays running with
    a heartbeat that will go stale, which is what the next worker looks for."""

    class _StopsItself(_Handler):
        def __init__(self, rows, worker_box):
            super().__init__(rows)
            self._box = worker_box

        def handle(self, store, user_id, row, plan):
            result = super().handle(store, user_id, row, plan)
            self._box[0].stop(timeout=0)  # a SIGTERM arriving mid-pass
            return result

    box = []
    handler = _StopsItself(_rows(4), box)
    worker = SweepWorker(runs, object(), [handler])
    box.append(worker)
    runs.enqueue(USER, "demo")

    worker.tick()

    run = runs.get(USER, "demo")
    assert run.state == STATE_RUNNING
    assert not run.finished
    assert len(handler.handled) == 1
    _age_heartbeat(runs)
    assert runs.get(USER, "demo").claimable


def test_the_inputs_are_read_once_per_attempt_not_once_per_row(runs):
    """Re-reading per row would add a round trip per memory, and would make
    the pass incoherent: a vocabulary change halfway through would be applied
    to later rows while every row is stamped with the earlier fingerprint."""

    class _Counting(_Handler):
        prepares = 0

        def prepare(self, store, user_id):
            type(self).prepares += 1
            return super().prepare(store, user_id)

    worker = SweepWorker(runs, object(), [_Counting(_rows(6))])
    runs.enqueue(USER, "demo")

    worker.tick()

    assert _Counting.prepares == 1
    assert runs.get(USER, "demo").processed == 6


def test_the_run_records_the_fingerprint_the_pass_ran_against(runs):
    worker = SweepWorker(runs, object(), [_Handler(_rows(2), fingerprint="vocab-7")])
    runs.enqueue(USER, "demo")

    worker.tick()

    assert runs.get(USER, "demo").fingerprint == "vocab-7"
