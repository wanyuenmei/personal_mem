"""The persisted record of a long pass over a user's store.

A sweep is minutes of work and one model call per memory. Holding that in a
thread and a dict — which is where both sweeps started — makes the run a
property of the process that happened to serve the POST: a deploy kills it
mid-pass, the progress the page was showing vanishes with it, and the only
way forward is to pay for the whole store again. This is the run as a row
instead, so it outlives the worker that is executing it.

One row per (user, kind). At most one run per user per kind was already the
rule — a second click while a pass is going should not double the API calls —
and a primary key is a better way to say it than a lock in one process.

Persistence follows the vector-store split rather than adding configuration,
exactly as ``ScopeRegistry`` does: SQLite in the local data dir, the deploy's
Postgres when VECTOR_STORE=pgvector, because container disk there is
ephemeral and a run that cannot survive a restart is the thing being fixed.

``fingerprint`` records what an attempt actually judged against — a hash of
the scope vocabulary, or the policy version for a pass with no vocabulary. It
is written when a worker starts the pass, not when the run is queued, and is
deliberately NOT read back to decide anything: a resume re-derives it from the
vocabulary as it stands now, because if that changed while the run was
stranded then a full pass is the right answer rather than a stale partial one.
What it buys is being able to tell, after the fact, which vocabulary a stored
result belongs to.

``detail`` is per-kind counters as JSON (scopes seen, memories set aside and
restored) rather than a column each: the lifecycle is shared, what a pass
counts is not, and a schema that grew a column per sweep kind would make
adding one a migration.
"""

import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from context_layer import config

# Lifecycle. ``queued`` is a run nobody has picked up yet; ``running`` is one a
# worker has claimed and is heartbeating. There is deliberately no "cancelled":
# nothing cancels a sweep today, and a state nothing writes is a state nothing
# tests.
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"

# Terminal states: a run in one of these is finished and a fresh enqueue may
# replace it.
_FINISHED = (STATE_DONE, STATE_ERROR)

# How long a ``running`` run may go without a heartbeat before another worker
# treats it as abandoned and resumes it. Comfortably longer than the heartbeat
# interval and than a single slow model call, so a worker that is merely busy
# is never stolen from; short enough that a deploy resumes in the next minute
# rather than the next hour.
STALE_AFTER = timedelta(minutes=5)

# ``error`` when every memory in a pass failed on its own rather than the pass
# itself blowing up — nearly always credentials or model configuration. The
# dashboard branches on this exact value to say so instead of showing an
# exception type.
ERROR_ALL_FAILED = "all_failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sweep_runs (
    user_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    state        TEXT NOT NULL,
    fingerprint  TEXT NOT NULL DEFAULT '',
    total        INTEGER NOT NULL DEFAULT 0,
    processed    INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    changed      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    detail       TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    queued_at    TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL DEFAULT '',
    finished_at  TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, kind)
)
"""

_COLUMNS = (
    "user_id, kind, state, fingerprint, total, processed, skipped, changed, "
    "failed, detail, error, queued_at, started_at, finished_at, heartbeat_at"
)

_SELECT = f"SELECT {_COLUMNS} FROM sweep_runs"

# A user's first ever run of a kind. DO NOTHING rather than DO UPDATE: if a
# row appeared between the read and this write, the reader's decision was made
# against a store that no longer exists and must not be applied.
_INSERT_IF_ABSENT = f"""
INSERT INTO sweep_runs ({_COLUMNS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (user_id, kind) DO NOTHING
"""

# Re-queueing over a finished or abandoned run. Guarded on exactly the row the
# caller read — the same compare-and-swap ``claim`` uses — so a worker that
# claimed an abandoned run in the meantime is not overwritten by a decision
# made before it did.
_REQUEUE = """
UPDATE sweep_runs SET
    state = ?, fingerprint = '', total = 0, processed = 0, skipped = 0,
    changed = 0, failed = 0, detail = '{}', error = '', queued_at = ?,
    started_at = '', finished_at = '', heartbeat_at = ''
WHERE user_id = ? AND kind = ? AND state = ? AND heartbeat_at = ?
"""


def now() -> str:
    """The timestamp format every column here stores: ISO-8601, UTC."""
    return datetime.now(timezone.utc).isoformat()


def _parse(stamp: str) -> Optional[datetime]:
    """A stored timestamp as a datetime, or None if it is empty or unreadable.

    Unreadable rather than raising: a heartbeat nobody can parse should make a
    run look abandoned — which is recoverable — not crash the worker that was
    trying to recover it.
    """
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SweepRun:
    """One pass over one user's store, as it is stored and as the page reads it."""

    user_id: str
    kind: str
    state: str = STATE_QUEUED
    fingerprint: str = ""
    total: int = 0
    processed: int = 0
    # Memories a run walked past because they were already current for this
    # fingerprint. Counted apart from ``processed`` so "3 of 900, 880 already
    # up to date" can be said out loud — the difference between a pass that is
    # nearly free and one that has barely started.
    skipped: int = 0
    changed: int = 0
    failed: int = 0
    detail: dict = field(default_factory=dict)
    error: str = ""
    queued_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    heartbeat_at: str = ""

    @property
    def finished(self) -> bool:
        return self.state in _FINISHED

    @property
    def abandoned(self) -> bool:
        """A run whose worker has stopped beating its heart.

        Only ever true of a ``running`` run: queued work has no worker yet, and
        finished work has no worker any more. An unreadable or missing
        heartbeat counts as abandoned — ``claim`` writes one, so a running run
        without a legible one is a row nothing is going to update again, and
        treating it as recoverable is the safe direction to be wrong in.
        """
        if self.state != STATE_RUNNING:
            return False
        beat = _parse(self.heartbeat_at)
        if beat is None:
            return True
        return datetime.now(timezone.utc) - beat > STALE_AFTER

    @property
    def claimable(self) -> bool:
        """Whether a worker may pick this up: nobody has, or nobody still is."""
        return self.state == STATE_QUEUED or self.abandoned

    @property
    def pending(self) -> bool:
        """Whether asking again would be asking for something already coming.

        A queued run is pending even though it is claimable — it has been asked
        for and no worker has needed to start it yet. An abandoned one is not:
        re-asking is how a user says "carry on with that".
        """
        return not self.finished and not self.abandoned

    def as_dict(self) -> dict:
        """What the page renders. ``detail`` is flattened in so a kind's own
        counters read like the shared ones rather than nested a level down."""
        data = {
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "skipped": self.skipped,
            "changed": self.changed,
            "failed": self.failed,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        data.update(self.detail)
        return data


class RunStore:
    """Sweep runs over SQLite (local) or Postgres (deploy).

    Exactly one of ``sqlite_path`` / ``database_url`` is set. Connections are
    opened per operation — a run writes a handful of rows a minute, so pooling
    would be dead weight — and the schema is created lazily on first use, the
    same shape ``ScopeRegistry`` uses.
    """

    def __init__(
        self,
        *,
        sqlite_path: Optional[str] = None,
        database_url: Optional[str] = None,
    ) -> None:
        if bool(sqlite_path) == bool(database_url):
            raise ValueError("provide exactly one of sqlite_path or database_url")
        self._sqlite_path = sqlite_path
        self._database_url = database_url
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    @classmethod
    def from_config(cls) -> "RunStore":
        """Backend follows the vector store, as the scope registry's does."""
        if config.VECTOR_STORE == "pgvector" and config.DATABASE_URL:
            return cls(database_url=config.DATABASE_URL)
        return cls(sqlite_path=os.path.join(config.DATA_DIR, "consent.db"))

    # --- storage plumbing -------------------------------------------------

    def _connect(self) -> Any:
        if self._database_url:
            import psycopg

            return psycopg.connect(self._database_url)
        assert self._sqlite_path is not None
        os.makedirs(os.path.dirname(self._sqlite_path), exist_ok=True)
        return sqlite3.connect(self._sqlite_path)

    def _sql(self, statement: str) -> str:
        """Statements are written with sqlite's ``?``; psycopg wants ``%s``."""
        if self._database_url:
            return statement.replace("?", "%s")
        return statement

    def _ensure_schema(self, conn) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.execute(_SCHEMA)
            conn.commit()
            self._schema_ready = True

    @staticmethod
    def _to_run(row: tuple) -> SweepRun:
        (user_id, kind, state, fingerprint, total, processed, skipped, changed,
         failed, detail, error, queued_at, started_at, finished_at,
         heartbeat_at) = row
        try:
            parsed = json.loads(detail) if detail else {}
        except ValueError:
            parsed = {}
        return SweepRun(
            user_id=user_id, kind=kind, state=state, fingerprint=fingerprint,
            total=total, processed=processed, skipped=skipped, changed=changed,
            failed=failed, detail=parsed if isinstance(parsed, dict) else {},
            error=error, queued_at=queued_at, started_at=started_at,
            finished_at=finished_at, heartbeat_at=heartbeat_at,
        )

    # --- operations -------------------------------------------------------

    def get(self, user_id: str, kind: str) -> Optional[SweepRun]:
        """This user's run of ``kind``, or None if they have never had one."""
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            cur = conn.execute(
                self._sql(f"{_SELECT} WHERE user_id = ? AND kind = ?"),
                (user_id, kind),
            )
            row = cur.fetchone()
            return self._to_run(row) if row else None

    def enqueue(self, user_id: str, kind: str) -> bool:
        """Ask for a pass. False when one is already queued or running.

        Refusing rather than queueing a second is the same rule the in-process
        runners had: the button is one click, and an impatient user should not
        be able to multiply the API calls. A run left ``running`` by a worker
        that died is NOT refused — it is abandoned, and re-asking is exactly
        how a user says "carry on with that".

        The write is a compare-and-swap against the row this call read, for the
        same reason ``claim`` is: those two decisions race precisely on the
        abandoned run they are both allowed to take. Without the guard, a
        re-ask that read the row a moment before a worker claimed it would
        stamp the row back to ``queued`` underneath that worker — and a second
        worker could then claim a pass already being executed.
        """
        stamp = now()
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            cur = conn.execute(
                self._sql(f"{_SELECT} WHERE user_id = ? AND kind = ?"),
                (user_id, kind),
            )
            row = cur.fetchone()
            existing = self._to_run(row) if row else None
            if existing is None:
                cur = conn.execute(
                    self._sql(_INSERT_IF_ABSENT),
                    (user_id, kind, STATE_QUEUED, "", 0, 0, 0, 0, 0, "{}", "",
                     stamp, "", "", ""),
                )
                conn.commit()
                return cur.rowcount == 1
            if existing.pending:
                return False
            cur = conn.execute(
                self._sql(_REQUEUE),
                (STATE_QUEUED, stamp, user_id, kind, existing.state,
                 existing.heartbeat_at),
            )
            conn.commit()
            return cur.rowcount == 1

    def claimable(self) -> list[SweepRun]:
        """Runs a worker may pick up: queued, or running with a dead heartbeat."""
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            cur = conn.execute(
                self._sql(f"{_SELECT} WHERE state IN (?, ?)"),
                (STATE_QUEUED, STATE_RUNNING),
            )
            return [
                run for run in (self._to_run(row) for row in cur.fetchall())
                if run.claimable
            ]

    def claim(self, run: SweepRun) -> Optional[SweepRun]:
        """Take ownership of ``run``, or None if someone got there first.

        The guard is the state and heartbeat this worker last saw: the UPDATE
        only lands if the row still looks the way it did when ``claimable``
        read it, so two workers racing for the same run cannot both win.
        Progress is reset because a resumed run re-walks from the top —
        already-current memories are skipped rather than re-classified, so the
        counters have to start from zero to describe THIS attempt.
        """
        started = now()
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            cur = conn.execute(
                self._sql(
                    "UPDATE sweep_runs SET state = ?, started_at = ?, "
                    "heartbeat_at = ?, processed = 0, skipped = 0, changed = 0, "
                    "failed = 0, error = '' "
                    "WHERE user_id = ? AND kind = ? AND state = ? "
                    "AND heartbeat_at = ?"
                ),
                (
                    STATE_RUNNING, started, started,
                    run.user_id, run.kind, run.state, run.heartbeat_at,
                ),
            )
            conn.commit()
            if cur.rowcount != 1:
                return None
        claimed = self.get(run.user_id, run.kind)
        return claimed if claimed and claimed.state == STATE_RUNNING else None

    def progress(
        self,
        user_id: str,
        kind: str,
        *,
        total: Optional[int] = None,
        fingerprint: Optional[str] = None,
        processed: Optional[int] = None,
        skipped: Optional[int] = None,
        changed: Optional[int] = None,
        failed: Optional[int] = None,
        detail: Optional[dict] = None,
    ) -> None:
        """Publish how far a claimed run has got, and beat its heart.

        Every call refreshes ``heartbeat_at``, so a worker that is making
        progress is never mistaken for one that died — the two facts are the
        same fact, and keeping them in one write means they cannot disagree.
        """
        sets = ["heartbeat_at = ?"]
        params: list[Any] = [now()]
        for name, value in (
            ("total", total), ("fingerprint", fingerprint),
            ("processed", processed), ("skipped", skipped),
            ("changed", changed), ("failed", failed),
        ):
            if value is not None:
                sets.append(f"{name} = ?")
                params.append(value)
        if detail is not None:
            sets.append("detail = ?")
            params.append(json.dumps(detail))
        params += [user_id, kind]
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            conn.execute(
                self._sql(
                    f"UPDATE sweep_runs SET {', '.join(sets)} "
                    "WHERE user_id = ? AND kind = ?"
                ),
                tuple(params),
            )
            conn.commit()

    def finish(
        self,
        user_id: str,
        kind: str,
        *,
        state: str,
        error: str = "",
        detail: Optional[dict] = None,
    ) -> None:
        """Close a run out, terminally."""
        sets = ["state = ?", "error = ?", "finished_at = ?", "heartbeat_at = ?"]
        stamp = now()
        params: list[Any] = [state, error, stamp, stamp]
        if detail is not None:
            sets.append("detail = ?")
            params.append(json.dumps(detail))
        params += [user_id, kind]
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            conn.execute(
                self._sql(
                    f"UPDATE sweep_runs SET {', '.join(sets)} "
                    "WHERE user_id = ? AND kind = ?"
                ),
                tuple(params),
            )
            conn.commit()


_NON_HEX = re.compile(r"[^0-9a-f]")


def fingerprint_of(*parts: str) -> str:
    """A short, stable hash of whatever a pass is judging against.

    Short because it is stored on every memory it stamps and read on every
    row a pass walks; stable because the same vocabulary must produce the same
    value across processes and restarts, which rules out anything seeded per
    run.
    """
    import hashlib

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return _NON_HEX.sub("", digest)[:16]
