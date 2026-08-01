"""The thing that actually runs a sweep, and picks one back up after a crash.

One background thread per process. It polls the run store for work — a run
somebody queued, or one a dead worker left half-finished — claims it, walks
the rows, and publishes progress as it goes. Nothing here decides WHEN a
sweep should happen: it only executes runs a person already asked for, so a
booting worker can finish an interrupted pass without any memory reaching a
model because a container restarted.

What a row means is not this module's business. A :class:`SweepHandler` says
which rows a pass covers, which of them are already current, and what judging
one does; the worker owns the lifecycle around that — the claim, the
heartbeat, the counters, the terminal state, and the rule that one row's
failure is counted rather than fatal.

The daemon thread is still a daemon thread: killing it on shutdown is fine
now, because what it was doing is a row in a table rather than state in this
process, and the next worker to boot will claim it back.
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from context_layer.jobs.runs import (
    ERROR_ALL_FAILED,
    STATE_DONE,
    STATE_ERROR,
    RunStore,
    SweepRun,
)

logger = logging.getLogger("context_layer.jobs.worker")

# How often an idle worker looks for work. A sweep is minutes long and started
# by hand, so seconds of latency before it begins are free; polling harder
# would just mean more queries against the run store for nothing.
POLL_SECONDS = 5.0

# How often progress is written while a pass runs. Every row would be a write
# per model call; never would let a live worker look abandoned. Once a second
# is far under the staleness window and far over the write cost.
HEARTBEAT_EVERY = 25


@dataclass(frozen=True)
class Pass:
    """Everything one attempt at a run needs, settled before the first row.

    Handed back by :meth:`SweepHandler.prepare` so the inputs a pass judges
    against are read ONCE and then held. That is not only cheaper than
    re-reading per row — it is what makes the pass coherent: every memory in
    this attempt is judged against the same vocabulary it gets stamped with,
    even if someone registers a scope halfway through.
    """

    fingerprint: str
    rows: Sequence[dict]
    # Per-kind counters at their starting values, so a run publishes a complete
    # shape from its first heartbeat rather than growing keys as they happen.
    # Some are facts about the input rather than the outcome — how many scopes
    # a tagging pass had to sort into — and are known before row one.
    detail: Mapping[str, int] = field(default_factory=dict)
    # Whatever the handler wants to carry from prepare() into handle(): the
    # scope list, a compiled policy, nothing at all.
    context: Any = None


class SweepHandler(Protocol):
    """What one kind of pass knows about itself.

    Deliberately small: the lifecycle is shared, the judgement is not.
    """

    kind: str

    def prepare(self, store, user_id: str) -> Pass:
        """Read the inputs once and settle what this attempt will do.

        Called exactly once per attempt, before any row is touched.
        """
        ...

    def is_current(self, row: dict, fingerprint: str) -> bool:
        """Whether this memory was already judged against ``fingerprint``."""
        ...

    def handle(self, store, user_id: str, row: dict, plan: Pass) -> Mapping[str, int]:
        """Judge one memory and write what changed.

        Returns counter deltas — ``{"changed": 1}``, ``{"archived": 1}`` —
        which the worker adds into the run's shared and per-kind counters.
        Raising means this row failed; the worker counts it and carries on.
        """
        ...


# Counter names the run row has columns for; anything else a handler returns
# lands in the JSON detail blob instead.
_SHARED = ("changed", "failed")


class SweepWorker:
    """Executes queued and abandoned runs, one at a time, forever."""

    def __init__(
        self,
        runs: RunStore,
        store,
        handlers: Sequence[SweepHandler],
        *,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._runs = runs
        self._store = store
        self._handlers = {handler.kind: handler for handler in handlers}
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Begin polling. Idempotent — a second call is a no-op, so wiring it
        into a composition root that runs twice under a reloader is safe."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="sweep-worker", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, *, timeout: float = 2.0) -> None:
        """Ask the loop to finish the row it is on and exit."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    # --- the loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.tick():
                    self._stop.wait(self._poll)
            except Exception:
                # A broken run store must not end the worker: the next tick may
                # well succeed, and a dead worker is how runs get stranded.
                logger.exception("sweep worker tick failed")
                self._stop.wait(self._poll)

    def tick(self) -> bool:
        """Claim and execute at most one run. True if there was work.

        Public so tests (and a future one-shot entry point) can drive the
        worker a step at a time instead of racing its thread.
        """
        for candidate in self._runs.claimable():
            handler = self._handlers.get(candidate.kind)
            if handler is None:
                # A kind this build doesn't know about — an older or newer
                # deploy's run. Leave it alone rather than failing it: the
                # process that understands it may still come back.
                continue
            claimed = self._runs.claim(candidate)
            if claimed is None:
                continue  # another worker won the race
            self._execute(handler, claimed)
            return True
        return False

    def _execute(self, handler: SweepHandler, run: SweepRun) -> None:
        user_id, kind = run.user_id, run.kind
        try:
            # Settled here rather than trusted from the queued row: a resume
            # judges against the vocabulary as it stands now, because if that
            # changed while the run was stranded then a full pass is the right
            # answer, not a stale partial one. Recorded on the run so a stored
            # result can be traced back to what produced it.
            plan = handler.prepare(self._store, user_id)
            detail = dict(plan.detail)
            rows = plan.rows
            self._runs.progress(
                user_id, kind, total=len(rows), detail=detail,
                fingerprint=plan.fingerprint,
            )
        except Exception as exc:
            logger.exception("sweep %s could not start for user=%s", kind, user_id)
            self._runs.finish(
                user_id, kind, state=STATE_ERROR, error=type(exc).__name__
            )
            return

        processed = skipped = changed = failed = 0
        for row in rows:
            if self._stop.is_set():
                # Shutting down mid-pass. Leave the run ``running`` with its
                # heartbeat where it is: it goes stale on its own and the next
                # worker resumes it, which is the whole point.
                logger.info(
                    "sweep %s interrupted for user=%s at %d/%d; it will resume",
                    kind, user_id, processed, len(rows),
                )
                self._runs.progress(
                    user_id, kind, processed=processed, skipped=skipped,
                    changed=changed, failed=failed, detail=detail,
                )
                return
            try:
                if handler.is_current(row, plan.fingerprint):
                    skipped += 1
                else:
                    for name, delta in handler.handle(
                        self._store, user_id, row, plan
                    ).items():
                        if name in _SHARED:
                            changed += delta if name == "changed" else 0
                            failed += delta if name == "failed" else 0
                        else:
                            detail[name] = detail.get(name, 0) + delta
            except Exception:
                failed += 1
                logger.exception(
                    "sweep %s failed on memory %r for user=%s",
                    kind, str(row.get("id") or ""), user_id,
                )
            processed += 1
            if processed % HEARTBEAT_EVERY == 0:
                self._runs.progress(
                    user_id, kind, processed=processed, skipped=skipped,
                    changed=changed, failed=failed, detail=detail,
                )

        self._runs.progress(
            user_id, kind, processed=processed, skipped=skipped,
            changed=changed, failed=failed, detail=detail,
        )
        # Every memory it actually tried failing is a broken model call, not a
        # store that needed nothing done; a pass that skipped everything is the
        # opposite — it succeeded at doing nothing, which is what an unchanged
        # store should cost.
        attempted = processed - skipped
        everything_failed = attempted > 0 and failed == attempted
        self._runs.finish(
            user_id, kind,
            state=STATE_ERROR if everything_failed else STATE_DONE,
            error=ERROR_ALL_FAILED if everything_failed else "",
            detail=detail,
        )
