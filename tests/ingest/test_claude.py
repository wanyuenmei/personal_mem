"""Tests for the Claude export parser (ingest/claude.py).

Pure Python against synthetic fixtures that mirror the documented Claude export
shape — no mem0, no LLM, no network, so these run fast and anywhere.
"""

import json
import zipfile

import pytest

from context_layer.ingest import (
    Conversation,
    parse_claude_conversations,
    parse_claude_export,
)
from context_layer.ingest.claude import CONVERSATIONS_FILENAME


def _export_payload() -> list:
    """A 2-conversation export exercising both message vintages: the current
    `content` blocks and the legacy flat `text` field."""
    return [
        {
            "uuid": "conv-1",
            "name": "Coffee preferences",
            "created_at": "2024-01-02T03:04:05.678Z",
            "updated_at": "2024-01-02T03:10:00.000Z",
            "chat_messages": [
                {
                    "uuid": "m1",
                    "sender": "human",
                    "text": "",
                    "created_at": "2024-01-02T03:04:05.678Z",
                    "content": [{"type": "text", "text": "I like dark roast."}],
                },
                {
                    "uuid": "m2",
                    "sender": "assistant",
                    "text": "Noted — dark roast it is.",  # legacy flat text
                    "created_at": "2024-01-02T03:04:30.000Z",
                    "content": [],
                },
            ],
        },
        {
            "uuid": "conv-2",
            "name": "Travel plans",
            "created_at": "2024-02-01T00:00:00.000Z",
            "updated_at": "2024-02-01T00:05:00.000Z",
            "chat_messages": [
                {
                    "uuid": "m3",
                    "sender": "human",
                    "content": [
                        {"type": "text", "text": "Planning a trip to Kyoto."},
                        {"type": "text", "text": "In spring."},
                    ],
                },
            ],
        },
    ]


def test_parses_conversations_and_maps_roles():
    convs = parse_claude_conversations(_export_payload())

    assert [c.source_id for c in convs] == ["conv-1", "conv-2"]
    assert all(c.source == "claude" for c in convs)

    first = convs[0]
    assert first.title == "Coffee preferences"
    assert first.created_at == "2024-01-02T03:04:05.678Z"
    assert first.updated_at == "2024-01-02T03:10:00.000Z"
    assert [(m.role, m.text) for m in first.messages] == [
        ("user", "I like dark roast."),
        ("assistant", "Noted — dark roast it is."),
    ]
    assert first.messages[0].created_at == "2024-01-02T03:04:05.678Z"


def test_joins_multiple_text_content_blocks():
    convs = parse_claude_conversations(_export_payload())

    kyoto = convs[1].messages[0]
    assert kyoto.role == "user"
    assert kyoto.text == "Planning a trip to Kyoto.\n\nIn spring."


def test_drops_empty_and_whitespace_messages():
    raw = [
        {
            "uuid": "c",
            "name": "n",
            "chat_messages": [
                {"sender": "human", "text": "   ", "content": []},
                {"sender": "assistant", "content": [{"type": "text", "text": "real"}]},
                {"sender": "human", "content": [{"type": "image", "source": "…"}]},
            ],
        }
    ]

    (conv,) = parse_claude_conversations(raw)

    assert [m.text for m in conv.messages] == ["real"]


def test_skips_unknown_sender():
    raw = [
        {
            "uuid": "c",
            "name": "n",
            "chat_messages": [
                {"sender": "system", "text": "you are a helpful assistant"},
                {"sender": "assistant", "text": "hi"},
            ],
        }
    ]

    (conv,) = parse_claude_conversations(raw)

    assert [(m.role, m.text) for m in conv.messages] == [("assistant", "hi")]


def test_tolerates_missing_fields():
    raw = [
        {"uuid": "c1"},  # no name, no timestamps, no chat_messages
        "not-a-dict",  # skipped entirely
        {"uuid": "c2", "name": None, "chat_messages": "unexpected-type"},
    ]

    convs = parse_claude_conversations(raw)

    assert [c.source_id for c in convs] == ["c1", "c2"]
    assert convs[0].title == ""
    assert convs[0].created_at is None
    assert convs[0].messages == []
    assert convs[1].title == ""  # name=None → ""
    assert convs[1].messages == []


def test_null_uuid_yields_empty_source_id_not_literal_none():
    """An explicit {"uuid": null} must give source_id "" (the missing-value
    case), never the literal string "None" — source_id is the idempotency key,
    so a "None" collision would fold distinct conversations into one."""
    (conv,) = parse_claude_conversations([{"uuid": None, "chat_messages": []}])

    assert conv.source_id == ""


def test_non_list_payload_raises():
    with pytest.raises(ValueError, match="JSON array"):
        parse_claude_conversations({"conversations": []})


def test_to_dict_round_trips_to_json():
    (conv, _) = parse_claude_conversations(_export_payload())

    as_dict = conv.to_dict()
    # Must be plain JSON-serializable (no dataclasses left in the tree).
    reloaded = json.loads(json.dumps(as_dict))

    assert reloaded["source"] == "claude"
    assert reloaded["source_id"] == "conv-1"
    assert reloaded["messages"][0] == {
        "role": "user",
        "text": "I like dark roast.",
        "created_at": "2024-01-02T03:04:05.678Z",
    }


# --- loader (parse_claude_export): file / directory / zip all agree ----------


def _expected() -> list[Conversation]:
    return parse_claude_conversations(_export_payload())


def test_parse_export_from_json_file(tmp_path):
    p = tmp_path / CONVERSATIONS_FILENAME
    p.write_text(json.dumps(_export_payload()), encoding="utf-8")

    assert parse_claude_export(p) == _expected()


def test_parse_export_from_directory(tmp_path):
    (tmp_path / CONVERSATIONS_FILENAME).write_text(
        json.dumps(_export_payload()), encoding="utf-8"
    )

    assert parse_claude_export(tmp_path) == _expected()


def test_parse_export_from_zip(tmp_path):
    zip_path = tmp_path / "claude-export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Under a top-level folder, as real exports nest it.
        zf.writestr(f"data-2024/{CONVERSATIONS_FILENAME}", json.dumps(_export_payload()))

    assert parse_claude_export(zip_path) == _expected()


def test_parse_export_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_claude_export(tmp_path / "nope.json")


def test_parse_export_directory_without_conversations_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match=CONVERSATIONS_FILENAME):
        parse_claude_export(tmp_path)
