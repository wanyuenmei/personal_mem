"""The LLM pass that decides which consent scopes a memory falls under.

One call per memory, carrying the user's WHOLE registered vocabulary as
``key: description`` lines and asking for the applicable keys back as a JSON
array. The vocabulary is per-user and changes whenever a party registers or
the user creates a scope, so it is passed in rather than baked in here — this
module knows how to ask the question, not what the categories are.

Multi-label, unlike the single-scope classifier this supersedes (VC-62,
canceled): a memory can be dietary *and* travel, and consent grants read the
tags independently. Returning nothing is a perfectly good answer — an
unclassifiable memory stays untagged rather than being forced into a bucket.

Privacy invariant, inherited from the rest of the store: this only calls out
when ``EXTRACTION_MODE == "anthropic"``. In ``none``/``ollama`` mode no
network call happens at all and every memory classifies as "no scopes" —
manual tagging from the dashboard still works, and the dashboard says so
rather than leaving the user wondering why the sweep button does nothing.

Every failure path returns "no scopes" instead of raising: an API error, a
reply that isn't JSON, or keys outside the vocabulary. Callers run on
background threads behind a write or a page POST, so a classifier problem
must degrade to "untagged", never to a failed write.
"""

import json
import logging
import re
from typing import Sequence

from context_layer import config
from context_layer.consent.registry import ConsentScope

logger = logging.getLogger("context_layer.consent.classifier")

# How much of a memory to send. Scope is a coarse signal and stored memories
# are short facts; truncating bounds the cost of a sweep over a large store.
MAX_CLASSIFY_CHARS = 2000

# Enough for a JSON array of scope keys and nothing else. The reply is data,
# not prose, so a small ceiling also caps a runaway generation.
_MAX_TOKENS = 256

# Strips a ```json … ``` fence, which models add even when told not to.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def classifier_enabled() -> bool:
    """Whether automatic classification can run at all.

    False outside ``EXTRACTION_MODE=anthropic``, where classifying would mean
    sending memories off the machine. Callers use this to skip the work
    entirely (no threads, no store reads) and to explain the state in the UI.
    """
    return config.EXTRACTION_MODE == "anthropic"


def _one_line(text: str) -> str:
    """``text`` with every run of whitespace collapsed to a single space."""
    return " ".join((text or "").split())


def vocabulary_lines(scopes: Sequence[ConsentScope]) -> str:
    """The registered vocabulary as one ``key: description`` line per scope.

    The KEY is what the model must echo back — it is the stable identifier
    tags are stored under, whereas display names collide across owners. The
    description (falling back to the display name) is what makes the key
    mean something to the model.

    Descriptions are written by whoever registered the scope, and a third
    party's registration is self-asserted (see ``registry``), so the text is
    flattened to a single line before it goes into the prompt. One line per
    scope is the format this list promises; a description carrying newlines
    would otherwise break that shape and let its author forge extra prompt
    structure to skew how the *user's* memories get tagged. Flattening is not
    a complete answer — a single line can still read as an instruction — but
    the blast radius is bounded to which *registered* keys get applied, since
    ``_parse`` drops anything outside the vocabulary. Treating these as
    genuinely untrusted prompt input is VC-89, to land before anything
    enforces access off the resulting tags.
    """
    return "\n".join(
        f"- {scope.key}: {_one_line(scope.description) or _one_line(scope.name)}"
        for scope in scopes
    )


def _prompt(snippet: str, scopes: Sequence[ConsentScope]) -> str:
    return (
        "You label personal-context memories with the categories they fall "
        "under.\n\nAvailable categories (identifier: what it covers):\n"
        f"{vocabulary_lines(scopes)}\n\n"
        "Reply with a JSON array of the identifiers that apply to the memory "
        "below — use only identifiers from the list above, include every one "
        "that genuinely applies, and reply with [] if none do. Output the "
        "JSON array and nothing else.\n\n"
        f"Memory:\n{snippet}"
    )


def _parse(raw: str, valid_keys: set[str]) -> list[str]:
    """The model's reply as an ordered, de-duplicated list of known keys.

    Anything unexpected — not JSON, not a list, keys the user never
    registered — is dropped rather than trusted, so a hallucinated category
    can never become a tag.
    """
    text = _FENCE_RE.sub("", raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        logger.warning("scope classifier returned non-JSON; treating as untagged")
        return []
    if not isinstance(parsed, list):
        logger.warning("scope classifier returned %s, not a list", type(parsed).__name__)
        return []
    keys = []
    for item in parsed:
        if isinstance(item, str) and item in valid_keys and item not in keys:
            keys.append(item)
    return keys


def classify(text: str, scopes: Sequence[ConsentScope]) -> list[str]:
    """The scope keys that apply to ``text``, drawn from ``scopes``.

    Returns ``[]`` — with no network call — when classification is disabled,
    when there is nothing to classify, or when no scopes are registered. Any
    error or unusable reply also returns ``[]``.
    """
    if not classifier_enabled():
        return []
    snippet = (text or "").strip()[:MAX_CLASSIFY_CHARS]
    if not snippet or not scopes:
        return []

    try:
        # Imported lazily: this module is imported by the dashboard and the
        # add path, neither of which should pay the SDK's import cost at
        # startup on a server that may never classify anything.
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            messages=[{"role": "user", "content": _prompt(snippet, scopes)}],
        )
        raw = "".join(
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
        )
    except Exception:
        logger.exception("scope classification failed; leaving the memory untagged")
        return []

    return _parse(raw, {scope.key for scope in scopes})
