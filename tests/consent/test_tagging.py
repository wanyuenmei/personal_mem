"""Where classifier output becomes tags: the reconcile rule, the sweep's own
half of the job handler, and the on-write pass.

The classifier itself is stubbed everywhere here (its own behaviour is
tests/consent/test_classifier.py) so these tests are about provenance and
threading: what gets written, what is left alone, and what must never be
allowed to fail a write.
"""

import time

import pytest

from context_layer.consent import (
    PROVENANCE_LLM,
    PROVENANCE_LLM_CLEARED,
    PROVENANCE_USER,
    PROVENANCE_USER_REMOVED,
    ClassificationFailed,
    ConsentScope,
    ScopeRegistry,
    tag_updates,
    tagging,
)
from context_layer.consent.tagging import (
    SWEPT_KEY,
    ScopeTaggingHandler,
    scope_fingerprint,
    tag_new_memories,
    tag_rows,
)

USER = "mei"

_DIETARY = ConsentScope(
    key="dietary__tastebuds",
    owner_type="third_party",
    owner_name="tastebuds",
    name="dietary",
    description="food",
)
_TRAVEL = ConsentScope(
    key="travel__user",
    owner_type="user",
    owner_name="user",
    name="travel",
    description="trips",
)
_SCOPES = [_DIETARY, _TRAVEL]


class _FakeStore:
    """Records update_metadata calls and serves rows to the sweep."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.updates = []

    def all(self, user_id, limit=1000):
        return self.rows

    def update_metadata(self, memory_id, updates, user_id=None):
        self.updates.append((memory_id, updates, user_id))
        return {"updated": True, "id": memory_id}


@pytest.fixture
def registry(tmp_path):
    """A real registry over temp SQLite, carrying one scope for "u1" — the
    handler and the on-write pass both read the vocabulary back out of it, so
    a stub would be testing the stub."""
    reg = ScopeRegistry(sqlite_path=str(tmp_path / "consent.db"))
    reg.register(
        "u1", owner_type="user", owner_slug="user", scopes=[("travel", "trips")]
    )
    return reg


@pytest.fixture
def stub_classify(monkeypatch):
    """Pin what the classifier "decides" for any text."""

    def install(keys):
        monkeypatch.setattr(tagging, "classify", lambda text, scopes: list(keys))

    return install


def _raising_classify(fails_on, keys=(_TRAVEL.key,)):
    """A classifier that fails for the given memory texts and picks ``keys``
    for the rest — how a bad API key or a rate limit looks from tag_rows."""

    def classify(text, scopes):
        if text in fails_on:
            raise ClassificationFailed("the scope classifier call failed")
        return list(keys)

    return classify


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- the reconcile rule ---------------------------------------------------


def test_a_picked_scope_is_tagged_llm():
    assert tag_updates({}, _SCOPES, ["travel__user"]) == {
        "cs_travel__user": PROVENANCE_LLM
    }


def test_a_manual_tag_survives_the_classifier():
    """A `user` tag is intent, not derivation: a sweep never rewrites it, even
    when the classifier agrees with it."""
    metadata = {"cs_travel__user": PROVENANCE_USER}

    assert tag_updates(metadata, _SCOPES, ["travel__user"]) == {}
    assert tag_updates(metadata, _SCOPES, []) == {}


def test_a_user_removed_tombstone_is_never_re_applied():
    """The whole point of the tombstone: the classifier vetoed once stays
    vetoed, however many sweeps run."""
    metadata = {"cs_dietary__tastebuds": PROVENANCE_USER_REMOVED}

    assert tag_updates(metadata, _SCOPES, ["dietary__tastebuds"]) == {}


def test_a_stale_llm_tag_is_retracted_not_deleted():
    """mem0 can't remove a metadata key, so an llm tag that no longer applies
    is written down to llm_cleared, which reads as untagged."""
    metadata = {"cs_travel__user": PROVENANCE_LLM}

    assert tag_updates(metadata, _SCOPES, []) == {
        "cs_travel__user": PROVENANCE_LLM_CLEARED
    }


def test_a_cleared_tag_can_come_back():
    """Unlike user_removed, llm_cleared carries no user intent — a later sweep
    may set it again."""
    metadata = {"cs_travel__user": PROVENANCE_LLM_CLEARED}

    assert tag_updates(metadata, _SCOPES, ["travel__user"]) == {
        "cs_travel__user": PROVENANCE_LLM
    }


def test_scopes_never_tagged_are_not_written_at_all():
    """Otherwise every memory would grow a key per registered scope."""
    assert tag_updates({}, _SCOPES, []) == {}


def test_an_unchanged_memory_produces_no_write():
    """A re-sweep of a settled store must not re-embed every memory."""
    metadata = {"cs_travel__user": PROVENANCE_LLM}

    assert tag_updates(metadata, _SCOPES, ["travel__user"]) == {}


# --- tag_rows -------------------------------------------------------------


def test_tag_rows_writes_through_the_tenant_guarded_primitive(stub_classify):
    stub_classify(["travel__user"])
    store = _FakeStore()

    counts = tag_rows(store, "u1", _SCOPES, [{"id": "m1", "memory": "went to Rome"}])

    assert (counts.changed, counts.failed) == (1, 0)
    # The stamp rides along with the tag: it is what lets the next pass skip
    # this memory instead of paying to classify it again.
    [(memory_id, written, user)] = store.updates
    assert (memory_id, user) == ("m1", "u1")
    assert written["cs_travel__user"] == PROVENANCE_LLM
    assert written[SWEPT_KEY] == scope_fingerprint(_SCOPES)


def test_one_memorys_failure_does_not_abandon_the_rest(stub_classify, caplog):
    stub_classify(["travel__user"])
    store = _FakeStore()
    failed = {"done": False}
    original = store.update_metadata

    def flaky(memory_id, updates, user_id=None):
        if memory_id == "m1" and not failed["done"]:
            failed["done"] = True
            raise RuntimeError("write lost a race with a delete")
        return original(memory_id, updates, user_id)

    store.update_metadata = flaky

    counts = tag_rows(
        store, "u1", _SCOPES,
        [{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}],
    )

    assert (counts.changed, counts.failed) == (1, 1)
    assert [call[0] for call in store.updates] == ["m2"]


def test_rows_without_text_or_id_are_skipped(stub_classify):
    stub_classify(["travel__user"])
    store = _FakeStore()

    counts = tag_rows(
        store, "u1", _SCOPES, [{"id": "m1", "memory": ""}, {"id": "", "memory": "x"}]
    )

    assert (counts.changed, counts.failed) == (0, 0)
    assert store.updates == []


def test_a_failed_classification_is_counted_not_read_as_no_scopes(monkeypatch):
    """The bug this replaces: the classifier returned `[]` for a failed call
    as well as for "nothing applies", so a broken one counted as a clean
    pass over every memory."""
    monkeypatch.setattr(tagging, "classify", _raising_classify({"a"}))
    store = _FakeStore()

    counts = tag_rows(store, "u1", _SCOPES, [{"id": "m1", "memory": "a"}])

    assert (counts.changed, counts.failed) == (0, 1)
    assert store.updates == []


def test_a_failed_classification_does_not_abandon_the_rest(monkeypatch):
    monkeypatch.setattr(tagging, "classify", _raising_classify({"a"}))
    store = _FakeStore()

    counts = tag_rows(
        store, "u1", _SCOPES,
        [{"id": "m1", "memory": "a"}, {"id": "m2", "memory": "b"}],
    )

    assert (counts.changed, counts.failed) == (1, 1)
    assert [call[0] for call in store.updates] == ["m2"]


# --- the sweep, as the job worker sees it ---------------------------------
#
# The lifecycle around this — claiming, resuming, counting, the all-failed
# rule — is the worker's and is tested in tests/jobs. What is consent-specific
# is what the pass judges against and what doing one memory means.


def test_the_fingerprint_covers_descriptions_not_just_keys(registry):
    """The classifier puts descriptions in the prompt, so editing one can
    change which memories a scope claims — every memory is owed a re-think."""
    registry.register(USER, owner_type="user", owner_slug="user",
                      scopes=[("dietary", "food")])
    store = _FakeStore([])
    before = ScopeTaggingHandler(registry).prepare(store, USER).fingerprint

    registry.register(USER, owner_type="user", owner_slug="user",
                      scopes=[("dietary", "food, allergies")])

    after = ScopeTaggingHandler(registry).prepare(store, USER).fingerprint
    assert after != before


def test_the_fingerprint_does_not_depend_on_registry_order():
    scopes = [
        ConsentScope(key="a__user", owner_type="user", owner_name="user",
                     name="a", description="x"),
        ConsentScope(key="b__user", owner_type="user", owner_name="user",
                     name="b", description="y"),
    ]

    assert scope_fingerprint(scopes) == scope_fingerprint(list(reversed(scopes)))


def test_a_memory_stamped_with_the_current_vocabulary_is_current(registry):
    handler = ScopeTaggingHandler(registry)

    assert handler.is_current({"metadata": {SWEPT_KEY: "fp"}}, "fp") is True
    assert handler.is_current({"metadata": {SWEPT_KEY: "old"}}, "fp") is False
    assert handler.is_current({"metadata": {}}, "fp") is False


def test_a_pass_with_no_registered_scopes_reads_no_memories(tmp_path):
    """Tags ARE scopes: with an empty registry every memory could only be
    decided "nothing applies", at the price of a model call each."""
    empty = ScopeRegistry(sqlite_path=str(tmp_path / "empty.db"))
    store = _FakeStore([{"id": "m1", "memory": "x"}])

    assert ScopeTaggingHandler(empty).prepare(store, USER).rows == []


def test_the_scope_count_is_known_before_the_first_row(registry):
    """It is a fact about the input, so it can be published from the first
    heartbeat — and stays true of a stored result afterwards (VC-90)."""
    registry.register(USER, owner_type="user", owner_slug="user",
                      scopes=[("dietary", ""), ("travel", "")])

    store = _FakeStore([])

    assert ScopeTaggingHandler(registry).prepare(store, USER).detail == {
        "scope_count": 2
    }


def test_handling_a_memory_stamps_it_even_when_no_tag_changed(registry, stub_classify):
    """The stamp is what makes the next pass free; it has to be written whether
    or not the verdict moved anything."""
    registry.register(USER, owner_type="user", owner_slug="user",
                      scopes=[("dietary", "")])
    stub_classify([])
    store = _FakeStore([])
    handler = ScopeTaggingHandler(registry)
    plan = handler.prepare(store, USER)

    counted = handler.handle(store, USER, {"id": "m1", "memory": "x"}, plan)

    assert counted == {}
    assert store.updates == [("m1", {SWEPT_KEY: plan.fingerprint}, USER)]


def test_handling_a_memory_that_gains_a_tag_counts_as_changed(registry, stub_classify):
    registry.register(USER, owner_type="user", owner_slug="user",
                      scopes=[("dietary", "")])
    stub_classify(["dietary__user"])
    store = _FakeStore([])
    handler = ScopeTaggingHandler(registry)
    plan = handler.prepare(store, USER)

    counted = handler.handle(store, USER, {"id": "m1", "memory": "x"}, plan)

    assert counted == {"changed": 1}
    written = store.updates[0][1]
    assert written["cs_dietary__user"] == "llm"
    assert written[SWEPT_KEY] == plan.fingerprint



# --- the on-write pass ----------------------------------------------------


def test_on_write_tagging_classifies_only_the_new_ids(
    registry, stub_classify, monkeypatch
):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    key = registry.all("u1")[0].key
    stub_classify([key])
    store = _FakeStore(
        [{"id": "m1", "memory": "new one"}, {"id": "m0", "memory": "already there"}]
    )

    thread = tag_new_memories(
        store, "u1", {"results": [{"id": "m1", "event": "ADD"}]}, registry=registry
    )
    assert thread is not None
    thread.join(timeout=5)

    assert [call[0] for call in store.updates] == ["m1"]


def test_on_write_tagging_reads_metadata_rather_than_trusting_the_add_result(
    registry, stub_classify, monkeypatch
):
    """An UPDATE event lands on a memory that may already carry a user tag the
    pass must not clobber — which is only visible by re-reading the row."""
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    key = registry.all("u1")[0].key
    stub_classify([key])
    store = _FakeStore(
        [{"id": "m1", "memory": "x", "metadata": {f"cs_{key}": PROVENANCE_USER_REMOVED}}]
    )

    thread = tag_new_memories(
        store, "u1", {"results": [{"id": "m1", "event": "UPDATE"}]}, registry=registry
    )
    assert thread is not None
    thread.join(timeout=5)

    # It writes — every judged memory is stamped with the vocabulary it was
    # judged against — but the write carries the stamp and nothing else: the
    # tombstone the user put there is untouched.
    [(_, written, _user)] = store.updates
    assert list(written) == [SWEPT_KEY]


def test_on_write_tagging_is_skipped_when_the_classifier_is_off(
    registry, monkeypatch
):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: False)
    store = _FakeStore([{"id": "m1", "memory": "x"}])

    assert (
        tag_new_memories(
            store, "u1", {"results": [{"id": "m1", "event": "ADD"}]}, registry=registry
        )
        is None
    )
    assert store.updates == []


def test_deleted_and_noop_events_are_not_classified(registry, monkeypatch):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    store = _FakeStore([{"id": "m1", "memory": "x"}])

    result = {"results": [{"id": "m1", "event": "DELETE"}, {"id": "m2", "event": "NONE"}]}

    assert tag_new_memories(store, "u1", result, registry=registry) is None
    assert store.updates == []


def test_on_write_tagging_never_raises_into_the_write_path(registry, monkeypatch):
    """add_memory has already saved the user's memory by this point; a
    classifier problem must not turn that into a failed tool call."""
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)

    class _Broken:
        def all(self, user_id, limit=1000):
            raise RuntimeError("backend down")

    thread = tag_new_memories(
        _Broken(), "u1", {"results": [{"id": "m1", "event": "ADD"}]}, registry=registry
    )
    if thread is not None:
        thread.join(timeout=5)


def test_a_failed_classification_never_raises_into_the_write_path(
    registry, monkeypatch
):
    """`classify` raising is the whole point of this change, and the on-write
    pass is the one caller that must still swallow it: the memory is already
    saved, and a broken classifier leaves it untagged for the next sweep."""
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    monkeypatch.setattr(tagging, "classify", _raising_classify({"x"}))
    store = _FakeStore([{"id": "m1", "memory": "x"}])

    thread = tag_new_memories(
        store, "u1", {"results": [{"id": "m1", "event": "ADD"}]}, registry=registry
    )
    assert thread is not None
    thread.join(timeout=5)

    assert store.updates == []


def test_a_malformed_add_result_is_survivable(registry, monkeypatch):
    monkeypatch.setattr(tagging, "classifier_enabled", lambda: True)
    store = _FakeStore()

    assert tag_new_memories(store, "u1", "not a result", registry=registry) is None
    assert tag_new_memories(store, "u1", {"results": None}, registry=registry) is None
