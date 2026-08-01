"""The retention vocabulary: how a state is read out of mem0 metadata.

Every test here is really the same test from a different angle — a memory is
kept unless something says clearly that it isn't. That is the direction the
failure mode has to point: a stray value showing a memory the user meant to
hide is a nuisance, and one hiding a memory they still have is a data loss
they can't see.
"""

from context_layer.curation import (
    REASON_KEY,
    REASON_MAX_CHARS,
    SOURCE_KEY,
    SOURCE_LLM,
    SOURCE_USER,
    STATE_ARCHIVED,
    STATE_KEEP,
    STATE_KEY,
    decided_by_user,
    is_archived,
    retention_reason,
    retention_state,
    state_updates,
)


def test_a_memory_with_no_retention_metadata_is_kept():
    """Every memory written before triage existed reads this way."""
    assert retention_state({}) == STATE_KEEP
    assert retention_state(None) == STATE_KEEP
    assert is_archived({}) is False


def test_an_archived_memory_reads_archived():
    assert is_archived({STATE_KEY: STATE_ARCHIVED}) is True


def test_an_unrecognized_state_reads_as_kept():
    """A future or buggy writer must not be able to hide a memory by writing a
    value nothing here understands."""
    assert retention_state({STATE_KEY: "quarantined"}) == STATE_KEEP
    assert retention_state({STATE_KEY: True}) == STATE_KEEP


def test_the_users_own_decision_is_recognizable():
    assert decided_by_user({SOURCE_KEY: SOURCE_USER}) is True
    assert decided_by_user({SOURCE_KEY: SOURCE_LLM}) is False
    assert decided_by_user({}) is False


def test_a_reason_is_read_back_and_a_non_string_one_is_not():
    assert retention_reason({REASON_KEY: "one-off task detail"}) == "one-off task detail"
    assert retention_reason({REASON_KEY: ["a", "list"]}) == ""
    assert retention_reason({}) == ""


def test_a_decision_writes_state_source_and_reason_together():
    assert state_updates(STATE_ARCHIVED, SOURCE_LLM, "trivia") == {
        STATE_KEY: STATE_ARCHIVED,
        SOURCE_KEY: SOURCE_LLM,
        REASON_KEY: "trivia",
    }


def test_a_reason_is_always_written_even_when_empty():
    """mem0 merges metadata and cannot remove a key, so restoring a memory has
    to overwrite the reason it was archived with, or the dashboard keeps
    explaining why a memory that is back is set aside."""
    assert state_updates(STATE_KEEP, SOURCE_USER)[REASON_KEY] == ""


def test_a_long_reason_is_bounded_at_the_write():
    assert len(state_updates(STATE_ARCHIVED, SOURCE_LLM, "w" * 900)[REASON_KEY]) == (
        REASON_MAX_CHARS
    )
