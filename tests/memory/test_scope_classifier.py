"""Tests for the shared scope classifier (memory/scope.py).

This is the path the live `add_memory` tool takes. The anthropic client is
faked by injecting a stand-in module into sys.modules, so these need neither
the real package nor a network call.
"""

import sys
from types import SimpleNamespace

import pytest

import context_layer.memory.scope as scope_mod
from context_layer.memory.scope import DEFAULT_SCOPE, MAX_CLASSIFY_CHARS, SCOPES, classify


def _install_fake_anthropic(monkeypatch, reply_text, *, capture=None):
    """Fake `anthropic` whose client returns `reply_text` as one text block.

    If `capture` is a dict, the create() kwargs land in it so a test can assert
    what was actually sent.
    """

    class _Block:
        type = "text"

        def __init__(self):
            self.text = reply_text

    class _Resp:
        def __init__(self):
            self.content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            if capture is not None:
                capture.update(kwargs)
            return _Resp()

    class _Anthropic:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    # A namespace is enough: `import anthropic` returns whatever sys.modules holds.
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=_Anthropic))


@pytest.mark.parametrize("mode", ["none", "ollama"])
def test_local_modes_return_default_without_calling_out(monkeypatch, mode):
    """The privacy invariant: nothing leaves the machine outside anthropic mode."""
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", mode)
    # No fake installed — reaching the client at all would fail here.
    monkeypatch.setitem(sys.modules, "anthropic", None)

    assert classify("I'm vegetarian and avoid gluten") == DEFAULT_SCOPE


def test_returns_validated_scope(monkeypatch):
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")
    _install_fake_anthropic(monkeypatch, "  Dietary\n")

    result = classify("I'm vegetarian and avoid gluten")

    assert result == "dietary"
    assert result in SCOPES


def test_out_of_vocabulary_reply_falls_back(monkeypatch):
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")
    _install_fake_anthropic(monkeypatch, "spaceships")

    assert classify("something unclassifiable") == DEFAULT_SCOPE


def test_client_error_falls_back_rather_than_blocking_the_write(monkeypatch):
    """A classification failure must never stop a memory being saved."""
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no api key")

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=_Boom))

    assert classify("anything") == DEFAULT_SCOPE


def test_blank_text_returns_default_without_calling_out(monkeypatch):
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")
    monkeypatch.setitem(sys.modules, "anthropic", None)

    assert classify("   \n  ") == DEFAULT_SCOPE


def test_long_text_is_truncated_before_being_sent(monkeypatch):
    """Scope is coarse, so only the opening is sent — keeps the call cheap."""
    monkeypatch.setattr(scope_mod, "EXTRACTION_MODE", "anthropic")
    sent = {}
    _install_fake_anthropic(monkeypatch, "work", capture=sent)

    classify("x" * (MAX_CLASSIFY_CHARS * 3))

    prompt = sent["messages"][0]["content"]
    # Count the payload, not the whole prompt — the template itself contains an x.
    assert "x" * MAX_CLASSIFY_CHARS in prompt
    assert "x" * (MAX_CLASSIFY_CHARS + 1) not in prompt
