"""Parse the Claude account data export into the normalized conversation format.

The Claude export (Settings → Privacy → Export data) arrives as a `.zip`
containing `conversations.json` (plus `projects.json`, `users.json`, which we
don't need here). `conversations.json` is a JSON array; each element looks like:

    {
      "uuid": "…",
      "name": "Conversation title",
      "created_at": "2024-01-02T03:04:05.678Z",
      "updated_at": "…",
      "chat_messages": [
        {
          "uuid": "…",
          "sender": "human" | "assistant",
          "text": "legacy flat text",
          "created_at": "…",
          "content": [{"type": "text", "text": "…"}, …]
        },
        …
      ]
    }

`content[]` is the current shape; older exports put the whole message in the
top-level `text`. We prefer joining the `text` content blocks and fall back to
the flat `text` field, so both vintages parse.

The parser is split in two: `parse_claude_conversations` is the pure core over
already-loaded JSON (fully unit-testable, no I/O); `parse_claude_export` is the
thin loader that accepts a file, a directory, or the raw `.zip`.
"""

import json
import zipfile
from pathlib import Path
from typing import Union

from context_layer.ingest.normalized import (
    ROLE_ASSISTANT,
    ROLE_USER,
    Conversation,
    Message,
)

SOURCE = "claude"

# The file inside the export (whether a directory or a .zip) that holds the
# conversation history.
CONVERSATIONS_FILENAME = "conversations.json"

# Claude's sender labels → our normalized roles. A sender not in this map
# (system notices, tooling, future labels) is skipped rather than guessed at.
_ROLE_BY_SENDER = {
    "human": ROLE_USER,
    "assistant": ROLE_ASSISTANT,
}


def _message_text(raw: dict) -> str:
    """Join the `text`-type content blocks; fall back to the flat `text` field.

    Returns "" when there's no textual content (e.g. an attachment-only or
    tool-only turn), which the caller drops.
    """
    blocks = raw.get("content")
    if isinstance(blocks, list):
        parts = [
            b["text"]
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if parts:
            return "\n\n".join(p for p in parts if p.strip())
    text = raw.get("text")
    return text if isinstance(text, str) else ""


def _parse_message(raw: dict) -> Union[Message, None]:
    """One export message → a normalized Message, or None to skip it.

    Skips messages with an unmapped sender or with no non-empty text.
    """
    if not isinstance(raw, dict):
        return None
    sender = raw.get("sender")
    role = _ROLE_BY_SENDER.get(sender) if isinstance(sender, str) else None
    if role is None:
        return None
    text = _message_text(raw)
    if not text.strip():
        return None
    created_at = raw.get("created_at")
    return Message(
        role=role,
        text=text,
        created_at=created_at if isinstance(created_at, str) else None,
    )


def _parse_conversation(raw: dict) -> Union[Conversation, None]:
    """One export conversation → a normalized Conversation, or None to skip a
    non-dict entry."""
    if not isinstance(raw, dict):
        return None
    raw_messages = raw.get("chat_messages")
    messages = []
    if isinstance(raw_messages, list):
        for m in raw_messages:
            parsed = _parse_message(m)
            if parsed is not None:
                messages.append(parsed)
    uuid = raw.get("uuid")
    name = raw.get("name")
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    return Conversation(
        source=SOURCE,
        # isinstance guard, not str(raw.get("uuid", "")): an explicit
        # {"uuid": None} would make str() yield the literal "None" (the get
        # default only applies to a *missing* key), and source_id is the
        # idempotency key later ingest keys on — a "None" collision would let
        # distinct conversations dedupe into one.
        source_id=uuid if isinstance(uuid, str) else "",
        title=name if isinstance(name, str) else "",
        messages=messages,
        created_at=created_at if isinstance(created_at, str) else None,
        updated_at=updated_at if isinstance(updated_at, str) else None,
    )


def parse_claude_conversations(raw: list) -> list[Conversation]:
    """Normalize an already-loaded `conversations.json` array.

    This is the pure core: no I/O, fully unit-testable. Non-dict entries are
    skipped; a payload that isn't a list at all raises ValueError (the usual
    cause is pointing at the wrong file in the export).
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"Expected the Claude export to be a JSON array of conversations, "
            f"got {type(raw).__name__}. Point at conversations.json, the export "
            f"directory, or the export .zip."
        )
    conversations = []
    for entry in raw:
        parsed = _parse_conversation(entry)
        if parsed is not None:
            conversations.append(parsed)
    return conversations


def _load_raw(source: Union[str, Path]) -> list:
    """Locate and load the `conversations.json` payload from a file, a
    directory containing it, or a `.zip` export archive."""
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"No such Claude export path: {path}")

    if path.is_dir():
        path = path / CONVERSATIONS_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"No {CONVERSATIONS_FILENAME} in export directory: {path.parent}"
            )

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            name = _conversations_member(zf.namelist())
            with zf.open(name) as f:
                return json.load(f)

    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _conversations_member(names: list[str]) -> str:
    """Find conversations.json inside a zip (it may sit under a top-level
    folder, e.g. `data-2024-.../conversations.json`)."""
    for name in names:
        if Path(name).name == CONVERSATIONS_FILENAME:
            return name
    raise FileNotFoundError(f"No {CONVERSATIONS_FILENAME} inside the export archive")


def parse_claude_export(source: Union[str, Path]) -> list[Conversation]:
    """Parse a Claude data export into normalized conversations.

    `source` may be the `conversations.json` file, the export directory that
    contains it, or the raw `.zip` archive.
    """
    return parse_claude_conversations(_load_raw(source))
