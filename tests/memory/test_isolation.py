"""Cross-tenant isolation test (PER-5).

Writes memories for two distinct user_ids directly against ContextStore, and
asserts search()/all() for one tenant never surfaces the other tenant's
memories. This holds regardless of how user_id gets resolved upstream —
identity.resolve_user_id() is a single-tenant stub today and PER-12 will wire
real auth on top of the same seam — because this test exercises the layer
where isolation actually needs to hold: the mem0-facing filter itself.

It also proves the PER-5 defense-in-depth guard: a falsy user_id
(None/""/whitespace) must raise loudly (TenantIsolationError) rather than
silently reaching mem0 with no per-tenant filter at all.

Uses a real local vector store (chroma) + real local embedder (fastembed,
EXTRACTION_MODE=none so no LLM/network call is made) rather than a mock —
see conftest.py for the environment setup this relies on.
"""

import pytest

from context_layer.memory import ContextStore, TenantIsolationError

USER_A = "isolation-test-user-a"
USER_B = "isolation-test-user-b"

FALSY_USER_IDS = [None, "", "   "]


@pytest.fixture(scope="module")
def store() -> ContextStore:
    return ContextStore()


@pytest.fixture(scope="module", autouse=True)
def _seed(store: ContextStore) -> None:
    """Write memories under two different tenants before any test runs.

    The two "secret" memories deliberately share almost all their vocabulary
    ("the user's pet is named ...") and differ only in the pet's name. That
    means a search query built from the shared words is a plausible semantic
    match for BOTH tenants' secret — so if the user_id filter were ever
    missing or broken, this test would actually see the leak, rather than the
    embedder itself coincidentally keeping the two tenants' texts apart.
    """
    store.add("The user's pet is named Biscuit.", user_id=USER_A, scope="general")
    store.add("The user prefers dark roast coffee, no sugar.", user_id=USER_A, scope="dietary")
    store.add("The user's pet is named Whiskers.", user_id=USER_B, scope="general")
    store.add("The user prefers green tea.", user_id=USER_B, scope="dietary")


def _texts(rows: list[dict]) -> list[str]:
    return [r.get("memory") or r.get("text") or "" for r in rows]


def test_search_never_crosses_tenants(store: ContextStore) -> None:
    a_results = store.search("what is the user's pet named?", user_id=USER_A, limit=10)
    b_results = store.search("what is the user's pet named?", user_id=USER_B, limit=10)

    a_texts = _texts(a_results)
    b_texts = _texts(b_results)

    assert any("Biscuit" in t for t in a_texts), a_texts
    assert not any("Whiskers" in t for t in a_texts), a_texts

    assert any("Whiskers" in t for t in b_texts), b_texts
    assert not any("Biscuit" in t for t in b_texts), b_texts


def test_get_all_never_crosses_tenants(store: ContextStore) -> None:
    a_all = _texts(store.all(user_id=USER_A))
    b_all = _texts(store.all(user_id=USER_B))

    assert a_all, "expected seeded memories for user A"
    assert b_all, "expected seeded memories for user B"

    assert not any("Whiskers" in t for t in a_all)
    assert not any("Biscuit" in t for t in b_all)
    # Belt-and-suspenders: no memory text should appear in both tenants' lists.
    assert not (set(a_all) & set(b_all))


@pytest.mark.parametrize("bad_user_id", FALSY_USER_IDS)
def test_add_rejects_falsy_user_id(store: ContextStore, bad_user_id) -> None:
    with pytest.raises(TenantIsolationError):
        store.add("this must never be written without a user_id", user_id=bad_user_id)


@pytest.mark.parametrize("bad_user_id", FALSY_USER_IDS)
def test_search_rejects_falsy_user_id(store: ContextStore, bad_user_id) -> None:
    with pytest.raises(TenantIsolationError):
        store.search("anything", user_id=bad_user_id)


@pytest.mark.parametrize("bad_user_id", FALSY_USER_IDS)
def test_all_rejects_falsy_user_id(store: ContextStore, bad_user_id) -> None:
    with pytest.raises(TenantIsolationError):
        store.all(user_id=bad_user_id)


def test_delete_removes_only_the_owners_memory(store: ContextStore) -> None:
    """Proves the tenant-safe delete against a real store: an owner can delete
    their own memory, and it actually disappears from get_all. Uses a dedicated
    user_id so it can't perturb the A/B seed the other tests assert on."""
    owner = "delete-test-owner"
    store.add("The user's lucky number is seven.", user_id=owner, scope="general")

    [row] = store.all(user_id=owner)
    result = store.delete(row["id"], user_id=owner)

    assert result == {"deleted": True, "id": row["id"]}
    assert store.all(user_id=owner) == []


def test_delete_refuses_to_cross_tenants(store: ContextStore) -> None:
    """A different tenant cannot delete a memory it doesn't own, even holding the
    exact id — the guard raises and the memory survives."""
    owner = "delete-test-owner-b"
    intruder = "delete-test-intruder"
    store.add("The user's passphrase is open sesame.", user_id=owner, scope="general")

    [row] = store.all(user_id=owner)
    with pytest.raises(TenantIsolationError):
        store.delete(row["id"], user_id=intruder)

    survivors = _texts(store.all(user_id=owner))
    assert any("open sesame" in t for t in survivors), survivors


def test_delete_all_wipes_only_the_targeted_tenant(store: ContextStore) -> None:
    """delete_all is scoped by the user_id filter: it must empty the victim's
    memories entirely while leaving another tenant's memories untouched."""
    victim = "delete-all-victim"
    bystander = "delete-all-bystander"
    store.add("The victim's first secret.", user_id=victim, scope="general")
    store.add("The victim's second secret.", user_id=victim, scope="dietary")
    store.add("The bystander's own secret.", user_id=bystander, scope="general")

    result = store.delete_all(user_id=victim)

    assert result == {"deleted_all": True, "user_id": victim, "count": 2}
    assert store.all(user_id=victim) == []
    assert _texts(store.all(user_id=bystander)), "bystander's memory must survive"


@pytest.mark.parametrize("bad_user_id", FALSY_USER_IDS)
def test_delete_rejects_falsy_user_id(store: ContextStore, bad_user_id) -> None:
    with pytest.raises(TenantIsolationError):
        store.delete("any-id", user_id=bad_user_id)


@pytest.mark.parametrize("bad_user_id", FALSY_USER_IDS)
def test_delete_all_rejects_falsy_user_id(store: ContextStore, bad_user_id) -> None:
    """The blast radius here is every tenant, so the guard must hold: a falsy
    user_id must never reach mem0's delete_all."""
    with pytest.raises(TenantIsolationError):
        store.delete_all(user_id=bad_user_id)
