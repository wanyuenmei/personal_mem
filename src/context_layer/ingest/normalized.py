"""The normalized conversation format every export parser targets.

One shared schema so downstream ingest code (the batch extraction runner, the
idempotency manifest) is written once against these types, not once per source.
Each parser (Claude here, ChatGPT next) maps its own export shape onto these
dataclasses; nothing downstream ever sees a source-specific sender label or
field name.

Kept deliberately small and dependency-free: plain dataclasses plus a
`to_dict()` for JSON serialization (inspection, round-trip tests, and a future
ingest manifest). No mem0/LLM/network involvement — this is an offline layer.
"""

from dataclasses import dataclass
from typing import Optional

# Normalized roles. Every source's sender label collapses to one of these, so
# downstream code never branches on "human" vs "user" vs "role: user".
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass
class Message:
    """One turn in a conversation, normalized across sources."""

    role: str  # ROLE_USER | ROLE_ASSISTANT
    text: str
    # ISO-8601 timestamp kept verbatim from the source (None if absent). We
    # don't parse it to a datetime here — the source string is the record;
    # downstream code can parse it if/when it needs ordering.
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {"role": self.role, "text": self.text, "created_at": self.created_at}


@dataclass
class Conversation:
    """A single conversation from an export, normalized across sources."""

    source: str  # e.g. "claude", "chatgpt"
    # The source's own stable id for this conversation (Claude's `uuid`). This
    # is the idempotency key the ingest manifest hashes on, so re-importing a
    # newer export skips conversations already ingested.
    source_id: str
    title: str
    messages: list[Message]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
        }
