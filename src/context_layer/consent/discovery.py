"""Where a user's scope vocabulary comes from in the first place.

The classifier can only tag memories into categories that already exist, so a
user who has never registered a scope gets a sweep that correctly does
nothing, forever. This module is the cold-start path out of that: read what is
already stored, propose categories for it, and let the user pick.

Two properties shape everything here.

BATCHED, not per-memory. One call carries a sample of the store and asks for
the handful of categories that cover it. Discovery is a question about the
shape of the whole store, unlike classification, which is a question about one
memory at a time — asking per memory would cost a full sweep's worth of calls
to produce a few category names, and each call would only see evidence for the
categories it could already name.

PROPOSED, never registered. :func:`suggest_scopes` returns candidates and
nothing else; only :func:`register_proposals`, called with the keys the user
ticked, writes to the registry. A scope key is the identity a future consent
grant gates on, so the vocabulary stays user-intentional: raw model output
never becomes durable registry state with no human in between. The rejected
shortcut — auto-creating scopes mid-sweep — also drifts, naming the same theme
``dietary__user`` on one run and ``food__user`` on the next, which silently
narrows any grant written against the earlier key.

Proposals are always the USER's own scopes (``owner_type="user"``, owner slug
``RESERVED_OWNER_SLUG``). A third party's vocabulary is its own to declare
through register_scopes; inventing one on its behalf would show the user a
party asking for context it never asked for.

Same privacy gate as the classifier: nothing is sent anywhere unless
``EXTRACTION_MODE=anthropic``, and the dashboard says so rather than offering
a button that cannot work.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from context_layer import config
from context_layer.consent.classifier import (
    classifier_enabled,
    one_line,
    parse_json_reply,
)
from context_layer.consent.registry import (
    DESCRIPTION_MAX_CHARS,
    RESERVED_OWNER_SLUG,
    SLUG_MAX_CHARS,
    ConsentScope,
    ScopeRegistry,
    scope_key,
    slugify,
)

logger = logging.getLogger("context_layer.consent.discovery")

# How much of the store one suggestion run looks at. A ceiling on both axes
# bounds the single call: themes worth a scope repeat, so a sample answers the
# question about as well as the whole store would.
MAX_SAMPLE_MEMORIES = 200
MAX_SNIPPET_CHARS = 300

# A vocabulary the user has to read and tick through, not an exhaustive
# taxonomy. Anything past this is dropped rather than shown.
MAX_PROPOSALS = 8

# Enough for MAX_PROPOSALS objects of a name and a sentence.
_MAX_TOKENS = 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ScopeProposal:
    """One candidate scope, before the user has decided anything about it."""

    name: str
    description: str

    @property
    def key(self) -> str:
        """The key this WOULD register under, if the user ticks it.

        Computed the same way the registry computes it, so the checklist the
        user ticks and the row that gets written are the same identity.
        """
        return scope_key(slugify(self.name), RESERVED_OWNER_SLUG)

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "description": self.description}


def sample_texts(rows: Sequence[dict]) -> list[str]:
    """Up to ``MAX_SAMPLE_MEMORIES`` memory texts, spread across ``rows``.

    A stride rather than the first N: the page hands over the whole store
    ordered by how mem0 returns it, and a prefix would propose categories for
    one slice of it (whatever that ordering favours) while ignoring the rest.
    Each text is flattened to one line and truncated — a memory carrying
    newlines would otherwise forge extra entries in the prompt's list.
    """
    texts = [one_line(str(row.get("memory") or row.get("text") or "")) for row in rows]
    texts = [text[:MAX_SNIPPET_CHARS] for text in texts if text]
    if len(texts) <= MAX_SAMPLE_MEMORIES:
        return texts
    stride = len(texts) / MAX_SAMPLE_MEMORIES
    return [texts[int(i * stride)] for i in range(MAX_SAMPLE_MEMORIES)]


def _prompt(snippets: Sequence[str], existing: Sequence[ConsentScope]) -> str:
    memories = "\n".join(f"- {snippet}" for snippet in snippets)
    already = ""
    if existing:
        names = "\n".join(f"- {one_line(scope.name)}" for scope in existing)
        already = (
            "\nThey already have these categories — do not propose these or "
            f"near-synonyms of them:\n{names}\n"
        )
    return (
        "You help someone organize their personal-context memories into "
        "consent scopes: named categories of personal context they can later "
        "choose to share with a particular app, one category at a time.\n\n"
        "Below is a sample of their stored memories. Propose at most "
        f"{MAX_PROPOSALS} categories covering the themes that actually recur "
        "in them — a category worth having covers several memories, not one. "
        "Each needs a short lowercase name (one or two words, e.g. "
        '"dietary", "work travel") and a one-sentence description of what '
        f"belongs in it.\n{already}\n"
        'Reply with a JSON array of objects, each {"name": ..., '
        '"description": ...}, most broadly useful first. Reply with [] if the '
        "memories support no clear category. Output the JSON array and "
        f"nothing else.\n\nMemories:\n{memories}"
    )


def _proposals_from(
    raw: str, existing: Sequence[ConsentScope]
) -> list[ScopeProposal]:
    """The usable proposals in a model reply.

    Raises on a reply that isn't a JSON array at all — a run that produced
    nothing usable must not be reported to the user as "no new categories".
    Individual malformed entries are dropped, as are proposals colliding with
    a scope the user already has: ``ScopeRegistry.register`` upserts, so
    registering a colliding key would overwrite a description someone wrote by
    hand with one a model invented.
    """
    parsed = parse_json_reply(raw)
    if not isinstance(parsed, list):
        raise ValueError("the scope suggestion reply was not a JSON array")
    taken = {slugify(scope.name) for scope in existing}
    proposals: list[ScopeProposal] = []
    for item in parsed:
        if len(proposals) >= MAX_PROPOSALS:
            break
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        description = item.get("description") or ""
        if not isinstance(name, str) or not isinstance(description, str):
            continue
        name = one_line(name)
        slug = slugify(name)
        # Over-long names are dropped rather than truncated: the display name
        # would then no longer be the thing its key was derived from. The
        # dashboard's own create-scope form applies the same ceiling.
        if not slug or slug in taken or len(name) > SLUG_MAX_CHARS:
            continue
        taken.add(slug)
        proposals.append(
            ScopeProposal(
                name=name,
                description=one_line(description)[:DESCRIPTION_MAX_CHARS],
            )
        )
    return proposals


def suggest_scopes(
    texts: Sequence[str], existing: Sequence[ConsentScope]
) -> list[ScopeProposal]:
    """Candidate scopes for ``texts``, excluding what ``existing`` covers.

    One call for the whole sample. Returns ``[]`` — with no network call —
    when suggesting is disabled or there is nothing to read.

    Unlike :func:`classify`, this does NOT swallow failures. The classifier
    runs behind a write and degrades to "untagged"; a suggestion run is a
    foreground action whose entire output is its result, so an API error or an
    unusable reply has to surface as a failed run rather than as an empty and
    apparently successful one.
    """
    if not classifier_enabled():
        return []
    snippets = [text for text in texts if text.strip()]
    if not snippets:
        return []

    # Imported lazily for the same reason the classifier does: the dashboard
    # imports this module, and a server that never suggests anything shouldn't
    # pay the SDK's import cost at startup.
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=_MAX_TOKENS,
        temperature=0,
        messages=[{"role": "user", "content": _prompt(snippets, existing)}],
    )
    raw = "".join(
        getattr(block, "text", "")
        for block in resp.content
        if getattr(block, "type", None) == "text"
    )
    return _proposals_from(raw, existing)


def register_proposals(
    registry: ScopeRegistry, user_id: str, proposals: Sequence[ScopeProposal]
) -> list[ScopeProposal]:
    """Register approved proposals as the user's own scopes; returns the
    ones actually written.

    Collisions are SKIPPED, never upserted. ``register`` upserts by design, so
    writing a proposal over an existing key would silently replace a
    description its owner wrote with one a model invented — and that key may
    already be the identity a grant was written against. Generation filters
    against the registry too; this is the re-check for the gap between
    proposing and confirming, in which a scope can be created by hand or
    registered by a party.
    """
    if not proposals:
        return []
    taken = {slugify(scope.name) for scope in registry.all(user_id)}
    fresh = [p for p in proposals if slugify(p.name) not in taken]
    skipped = len(proposals) - len(fresh)
    if skipped:
        logger.info(
            "skipped %d suggested scope(s) already in the registry for user=%s",
            skipped, user_id,
        )
    if fresh:
        registry.register(
            user_id,
            owner_type="user",
            owner_slug=RESERVED_OWNER_SLUG,
            scopes=[(p.name, p.description) for p in fresh],
        )
    return fresh


# --- the user-triggered suggestion run -------------------------------------


@dataclass
class SuggestionStatus:
    """What the dashboard shows about a user's suggestion run.

    In-process and not persisted, like the sweep's status: it describes a
    thread in THIS worker. Losing it on restart costs a re-run, and losing
    *proposals* is the right failure — an un-approved proposal is not state
    worth keeping, it is a question the user hasn't answered yet.
    """

    state: str = "idle"  # idle | running | done | error
    sampled: int = 0
    proposals: list[ScopeProposal] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "sampled": self.sampled,
            "proposals": [p.as_dict() for p in self.proposals],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class SuggestionRunner:
    """Per-user suggestion threads plus the proposals awaiting approval.

    Backgrounded for the same reason the sweep is: the call is bounded but not
    fast, and the page reads the result on reload rather than holding a
    request open. At most one run per user — a second click while one is in
    flight is a no-op, not a second batch of API tokens.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[str, SuggestionStatus] = {}

    def status(self, user_id: str) -> SuggestionStatus:
        with self._lock:
            return self._statuses.get(user_id) or SuggestionStatus()

    def start(self, store, registry: ScopeRegistry, user_id: str) -> bool:
        """Kick off a run; False if one is already running for this user.

        Claims the slot under the lock before the thread starts, so two
        near-simultaneous POSTs can't both see "idle" and both spawn.
        """
        with self._lock:
            existing = self._statuses.get(user_id)
            if existing is not None and existing.state == "running":
                return False
            self._statuses[user_id] = SuggestionStatus(
                state="running", started_at=_now()
            )
        thread = threading.Thread(
            target=self._run,
            args=(store, registry, user_id),
            name=f"scope-suggest-{user_id}",
            daemon=True,
        )
        thread.start()
        return True

    def take(self, user_id: str, keys: Sequence[str]) -> list[ScopeProposal]:
        """Consume this user's proposals, returning the ones they ticked.

        The whole set is cleared: answering the question ends it, and the
        proposals left unticked were declined rather than deferred. A run
        still in flight is left alone so its result isn't dropped.
        """
        wanted = set(keys)
        with self._lock:
            status = self._statuses.get(user_id)
            if status is None or status.state == "running":
                return []
            del self._statuses[user_id]
        return [p for p in status.proposals if p.key in wanted]

    def _update(self, user_id: str, **fields) -> None:
        with self._lock:
            status = self._statuses.get(user_id)
            if status is None:
                return
            for name, value in fields.items():
                setattr(status, name, value)

    def _run(self, store, registry: ScopeRegistry, user_id: str) -> None:
        try:
            existing = registry.all(user_id)
            texts = sample_texts(store.all(user_id))
            proposals = suggest_scopes(texts, existing)
            self._update(
                user_id,
                state="done",
                sampled=len(texts),
                proposals=proposals,
                finished_at=_now(),
            )
        except Exception as exc:
            logger.exception("scope suggestion failed for user=%s", user_id)
            self._update(
                user_id,
                state="error",
                error=type(exc).__name__,
                finished_at=_now(),
            )


_suggestions = SuggestionRunner()


def get_suggestion_runner() -> SuggestionRunner:
    """The process-wide runner the dashboard starts and reads."""
    return _suggestions
