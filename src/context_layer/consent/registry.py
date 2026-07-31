"""The per-user registry of consent scopes.

Each scope belongs to an owner: a third party (which registers the categories
of context it cares about via the register_scopes tool) or the user themselves
(scopes they create for their own organization). A scope's identity is the
deterministic key ``<name-slug>__<owner-slug>`` — human-readable, stable across
re-registration, and safe to use later as a mem0 payload key (the tagging
layer stores one payload key per scope precisely because mem0's pgvector
filter builder cannot match values inside a JSON array).

The registry is PER USER: every row is keyed by the same user_id the memory
store partitions on, and a third party's registration is only visible to the
user whose connection it arrived through. That is deliberate. Until token
introspection (VC-65) gives us a verified OAuth client identity, a party name
is self-asserted by the calling client, so a global registry would let any
client pollute or squat another party's vocabulary across all tenants. When
real client identity lands, promoting this to a global registry is a data
migration, not a redesign.

Persistence follows the vector-store split rather than adding new
configuration: SQLite in the local data dir alongside the Chroma store, and
the deploy's Postgres (DATABASE_URL, already serving pgvector) when
VECTOR_STORE=pgvector — container disk on the deploy is ephemeral, so Postgres
is the only durable choice there. Plain DB-API SQL; the schema is one table
and an ORM would outweigh it.
"""

import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from context_layer import config

# Owner slug reserved for scopes the user creates for themselves. A third
# party may not register under it — that would let a client masquerade its
# vocabulary as the user's own.
RESERVED_OWNER_SLUG = "user"

# A slug is the canonical, filter-safe form of a scope or owner name:
# lowercase alphanumerics and single hyphens, bounded length. Everything else
# collapses to hyphens so "Dietary Prefs!" and "dietary-prefs" are the same
# scope rather than two.
SLUG_MAX_CHARS = 40
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Descriptions are shown to the user and bounded so no registration surface
# (the register_scopes tool, the dashboard's create-scope form) can stuff
# unbounded text into the registry.
#
# A third party's description is self-asserted, and it does not only get
# displayed: the scope classifier puts it in the prompt that decides which of
# the user's memories carry this scope. That makes it untrusted input to a
# prompt, bounded here by length and flattened to one line at the call site,
# but not otherwise validated — VC-89 covers doing that properly, before
# anything enforces access off these tags.
DESCRIPTION_MAX_CHARS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consent_scopes (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    owner_type  TEXT NOT NULL,
    owner_name  TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
)
"""

_UPSERT = """
INSERT INTO consent_scopes
    (user_id, key, owner_type, owner_name, name, description, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (user_id, key) DO UPDATE SET
    name = excluded.name,
    description = excluded.description,
    updated_at = excluded.updated_at
"""

_SELECT = (
    "SELECT key, owner_type, owner_name, name, description "
    "FROM consent_scopes WHERE user_id = ?"
)

_DELETE = "DELETE FROM consent_scopes WHERE user_id = ? AND key = ?"


def slugify(raw: str) -> str:
    """Canonical slug for a scope or owner name; "" if nothing survives."""
    slug = _NON_SLUG_RE.sub("-", raw.strip().lower()).strip("-")
    return slug[:SLUG_MAX_CHARS].rstrip("-")


def scope_key(name_slug: str, owner_slug: str) -> str:
    """The scope's identity: ``<name-slug>__<owner-slug>``."""
    return f"{name_slug}__{owner_slug}"


@dataclass(frozen=True)
class ConsentScope:
    """One registered scope, as read back from the registry."""

    key: str
    owner_type: str  # "third_party" | "user"
    owner_name: str  # owner slug: the party's slug, or RESERVED_OWNER_SLUG
    name: str  # display name as registered, pre-slugging
    description: str


def _require_user_id(user_id: Optional[str], *, op: str) -> str:
    """Same tenant guard shape as the memory store: refuse a falsy user_id
    loudly rather than ever touching rows without a per-tenant filter."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError(
            f"Refusing to {op}: user_id must be a non-empty string, got "
            f"{user_id!r} (tenant-isolation guard)."
        )
    return user_id


class ScopeRegistry:
    """Per-user consent-scope storage over SQLite (local) or Postgres (deploy).

    Exactly one of ``sqlite_path`` / ``database_url`` is set. Connections are
    opened per operation — registrations are rare and single-row, so pooling
    would be dead weight — and the schema is created lazily on first use.
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
    def from_config(cls) -> "ScopeRegistry":
        """Backend follows the vector store: Postgres wherever pgvector runs
        (the deploy, where local disk is ephemeral), SQLite otherwise."""
        if config.VECTOR_STORE == "pgvector" and config.DATABASE_URL:
            return cls(database_url=config.DATABASE_URL)
        return cls(sqlite_path=os.path.join(config.DATA_DIR, "consent.db"))

    # --- storage plumbing -------------------------------------------------

    def _connect(self) -> Any:
        """A DB-API connection — sqlite3.Connection or psycopg.Connection.

        Typed Any: the two backends' type stubs disagree (psycopg's execute()
        wants LiteralString), and this module only uses the DB-API surface
        they share.
        """
        if self._database_url:
            import psycopg

            return psycopg.connect(self._database_url)
        assert self._sqlite_path is not None
        os.makedirs(os.path.dirname(self._sqlite_path), exist_ok=True)
        return sqlite3.connect(self._sqlite_path)

    def _sql(self, statement: str) -> str:
        """Shared statements are written with sqlite's ``?`` placeholders;
        psycopg wants ``%s``. The queries hold no literal ``?``/``%s``, so a
        straight replace is safe."""
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

    # --- operations -------------------------------------------------------

    def register(
        self,
        user_id: Optional[str],
        *,
        owner_type: str,
        owner_slug: str,
        scopes: Iterable[tuple[str, str]],
    ) -> list[ConsentScope]:
        """Upsert ``(name, description)`` pairs for one owner and return the
        owner's full scope list afterwards.

        Upsert-only by design: re-registering refreshes names/descriptions but
        never implicitly deletes a scope the owner stopped sending — a future
        grant may reference its key, and silently dropping a key a user
        consented under is a consent problem, not a cleanup.
        """
        user_id = _require_user_id(user_id, op="register consent scopes")
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for name, description in scopes:
            name_slug = slugify(name)
            if not name_slug:
                raise ValueError(f"scope name {name!r} has no valid characters")
            rows.append(
                (
                    user_id,
                    scope_key(name_slug, owner_slug),
                    owner_type,
                    owner_slug,
                    name.strip(),
                    description.strip(),
                    now,
                    now,
                )
            )
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            for row in rows:
                conn.execute(self._sql(_UPSERT), row)
            conn.commit()
            return self._list(conn, user_id, owner_slug=owner_slug)

    def all(self, user_id: Optional[str]) -> list[ConsentScope]:
        """Every scope registered for this user, across all owners.

        Named like ContextStore.all — and not ``list``, which as a class-body
        name would shadow the builtin inside later method annotations.
        """
        user_id = _require_user_id(user_id, op="list consent scopes")
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            return self._list(conn, user_id)

    def get(self, user_id: Optional[str], key: str) -> Optional[ConsentScope]:
        """One scope by key, or None."""
        user_id = _require_user_id(user_id, op="look up a consent scope")
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            cur = conn.execute(self._sql(_SELECT + " AND key = ?"), (user_id, key))
            row = cur.fetchone()
            return self._to_scope(row) if row else None

    def delete(self, user_id: Optional[str], key: str) -> bool:
        """Remove one scope row; True if a row was actually deleted.

        This does not contradict the upsert-only registration rule: that rule
        stops a PARTY from silently retracting a scope by omitting it from a
        re-registration. Deletion here is an explicit act by the scope's
        user-side owner — the dashboard exposes it only for scopes the user
        created themselves. Tags referencing a deleted key stay in memory
        metadata but read as untagged (rendering and enforcement join against
        the registry).
        """
        user_id = _require_user_id(user_id, op="delete a consent scope")
        with closing(self._connect()) as conn:
            self._ensure_schema(conn)
            cur = conn.execute(self._sql(_DELETE), (user_id, key))
            conn.commit()
            return cur.rowcount > 0

    def _list(
        self, conn, user_id: str, *, owner_slug: Optional[str] = None
    ) -> list[ConsentScope]:
        statement, params = _SELECT, [user_id]
        if owner_slug is not None:
            statement += " AND owner_name = ?"
            params.append(owner_slug)
        statement += " ORDER BY owner_name, name"
        cur = conn.execute(self._sql(statement), tuple(params))
        return [self._to_scope(row) for row in cur.fetchall()]

    @staticmethod
    def _to_scope(row: tuple) -> ConsentScope:
        key, owner_type, owner_name, name, description = row
        return ConsentScope(
            key=key,
            owner_type=owner_type,
            owner_name=owner_name,
            name=name,
            description=description,
        )
