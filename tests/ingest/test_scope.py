"""Tests for conversation scope inference (ingest/scope.py).

The classifier itself lives in memory/scope.py (covered directly in
tests/memory/test_scope_classifier.py); these check that ingest flattens a conversation
and delegates to it correctly.

The anthropic client is faked by injecting a stand-in `anthropic` module into
sys.modules, so these need neither the real package nor a network call.
"""

import sys
import types

import context_layer.memory.scope as scope_mod
from context_layer.ingest.normalized import Conversation, Message
from context_layer.ingest.scope import SCOPES, infer_scope


def _conv():
    return Conversation(
        source="claude",
        source_id="c1",
        title="Dinner ideas",
        messages=[Message(role="user", text="I'm vegetarian and avoid gluten")],
    )


def _install_fake_anthropic(monkeypatch, reply_text):
    """Put a fake `anthropic` module in sys.modules whose client returns
    `reply_text` as the single text content block."""

    class _Block:
        type = "text"

        def __init__(self):
            self.text = reply_text

    class _Resp:
        def __init__(self):
            self.content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            return _Resp()

    class _Anthropic:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)


def test_none_mode_returns_default_without_calling_out(monkeypatch):
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "none")
    # No fake installed: if it tried to import/construct a client it would use
    # the real (or missing) anthropic — the point is it must return before then.
    monkeypatch.setitem(sys.modules, "anthropic", None)

    assert infer_scope(_conv(), default="general") == "general"


def test_anthropic_path_returns_validated_scope(monkeypatch):
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")
    _install_fake_anthropic(monkeypatch, "  Dietary\n")

    result = infer_scope(_conv())

    assert result == "dietary"
    assert result in SCOPES


def test_out_of_vocabulary_reply_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")
    _install_fake_anthropic(monkeypatch, "spaceships")

    assert infer_scope(_conv(), default="general") == "general"


def test_client_error_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no api key")

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Boom
    monkeypatch.setitem(sys.modules, "anthropic", mod)

    assert infer_scope(_conv(), default="general") == "general"
