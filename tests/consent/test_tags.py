"""The cs_* tag vocabulary: key shape and how metadata reads back as tags."""

from context_layer.consent import (
    PROVENANCE_LLM,
    PROVENANCE_USER,
    PROVENANCE_USER_REMOVED,
    active_tags,
    tag_key,
)


def test_tag_key_prefixes_the_scope_key():
    assert tag_key("dietary__tastebuds") == "cs_dietary__tastebuds"


def test_active_tags_reads_only_live_cs_keys():
    metadata = {
        "source": "claude",
        "cs_dietary__tastebuds": PROVENANCE_USER,
        "cs_travel__tripapp": PROVENANCE_LLM,
        "cs_health__user": PROVENANCE_USER_REMOVED,
    }

    assert active_tags(metadata) == {
        "dietary__tastebuds": PROVENANCE_USER,
        "travel__tripapp": PROVENANCE_LLM,
    }


def test_active_tags_ignores_non_string_and_unknown_values():
    """A tag value must be one of the known provenance strings — an array (the
    exact shape mem0's pgvector filters can't match) or junk reads as untagged
    rather than being trusted."""
    metadata = {
        "cs_dietary__tastebuds": ["user"],
        "cs_travel__tripapp": "definitely",
    }

    assert active_tags(metadata) == {}


def test_active_tags_of_missing_metadata_is_empty():
    assert active_tags(None) == {}
    assert active_tags({}) == {}
