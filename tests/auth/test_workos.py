"""Unit tests for the WorkOS OAuth resource-server wiring.

Token verification is exercised end-to-end (signature -> claim validation ->
principal) using locally-minted HS256 tokens and an injected signing-key
resolver, so no network or real WorkOS credentials are needed. Production uses
RS256 against WorkOS's JWKS; the validation logic under test is identical.
"""

import asyncio
import time

import jwt

from context_layer import identity
from context_layer.auth import (
    WorkOSTokenVerifier,
    build_fastmcp_auth_kwargs,
    jwks_url_for,
)
from context_layer.config import DEFAULT_USER_ID

SECRET = "unit-test-signing-secret-not-a-credential-padded-past-32-bytes"
ISSUER = "https://example.authkit.app"


def verify(verifier, token):
    """Drive the async verify_token synchronously (no pytest-asyncio needed)."""
    return asyncio.run(verifier.verify_token(token))


def make_verifier(**overrides):
    kwargs = dict(
        issuer=ISSUER,
        signing_key_resolver=lambda _token: SECRET,
        algorithms=("HS256",),
    )
    kwargs.update(overrides)
    return WorkOSTokenVerifier(**kwargs)


def mint(claims, *, secret=SECRET, alg="HS256"):
    return jwt.encode(claims, secret, algorithm=alg)


def base_claims(**overrides):
    now = int(time.time())
    claims = {"sub": "user_01ABC", "iss": ISSUER, "iat": now, "exp": now + 3600}
    claims.update(overrides)
    return claims


def test_valid_token_returns_access_token():
    v = make_verifier()
    tok = mint(base_claims(scope="read write", client_id="client_123"))
    result = verify(v, tok)
    assert result is not None
    assert result.subject == "user_01ABC"
    assert result.client_id == "client_123"
    assert set(result.scopes) == {"read", "write"}
    assert result.expires_at is not None
    assert result.claims is not None
    assert result.claims["iss"] == ISSUER


def test_expired_token_rejected():
    v = make_verifier()
    assert verify(v, mint(base_claims(exp=int(time.time()) - 10))) is None


def test_malformed_token_rejected():
    v = make_verifier()
    assert verify(v, "not.a.jwt") is None
    assert verify(v, "") is None
    assert verify(v, "garbage") is None


def test_wrong_issuer_rejected():
    v = make_verifier()
    assert verify(v, mint(base_claims(iss="https://evil.example.com"))) is None


def test_bad_signature_rejected():
    v = make_verifier()
    tok = mint(base_claims(), secret="a-different-secret-also-padded-well-past-32b")
    assert verify(v, tok) is None


def test_wrong_audience_rejected_when_audience_required():
    v = make_verifier(audience="my-resource")
    assert verify(v, mint(base_claims(aud="some-other-resource"))) is None


def test_correct_audience_accepted():
    v = make_verifier(audience="my-resource")
    result = verify(v, mint(base_claims(aud="my-resource")))
    assert result is not None
    assert result.subject == "user_01ABC"


def test_client_id_falls_back_to_azp():
    v = make_verifier()
    result = verify(v, mint(base_claims(azp="client_from_azp")))
    assert result is not None
    assert result.client_id == "client_from_azp"


def test_client_id_is_unknown_when_no_client_claim():
    """WorkOS MCP-flow tokens carry neither claim. Reporting the issuer here
    produced a constant that read as real per-app data in the access log."""
    v = make_verifier()
    result = verify(v, mint(base_claims()))
    assert result is not None
    assert result.client_id == "unknown"
    assert result.client_id != ISSUER
    assert result.client_id != result.subject


def test_missing_subject_rejected():
    v = make_verifier()
    claims = base_claims()
    del claims["sub"]
    assert verify(v, mint(claims)) is None


def test_required_scopes_enforced():
    v = make_verifier(required_scopes=["read", "admin"])
    # only `read` present -> missing `admin` -> rejected
    assert verify(v, mint(base_claims(scope="read"))) is None
    # both present -> accepted
    assert verify(v, mint(base_claims(scope="read admin"))) is not None


def test_scopes_as_list_claim():
    v = make_verifier()
    result = verify(v, mint(base_claims(scopes=["a", "b"])))
    assert result is not None
    assert set(result.scopes) == {"a", "b"}


def test_issuer_trailing_slash_tolerated():
    # WorkOS's issuer has no trailing slash, but a stray one on either side must
    # not turn a valid token into a 401.
    v = make_verifier(issuer="https://example.authkit.app")
    assert verify(v, mint(base_claims(iss="https://example.authkit.app/"))) is not None
    v2 = make_verifier(issuer="https://example.authkit.app/")
    assert verify(v2, mint(base_claims(iss="https://example.authkit.app"))) is not None


def test_leeway_allows_small_clock_skew():
    now = int(time.time())
    # Expired 10s ago: rejected with no leeway, accepted within a 30s tolerance.
    assert verify(make_verifier(), mint(base_claims(exp=now - 10))) is None
    assert verify(make_verifier(leeway=30), mint(base_claims(exp=now - 10))) is not None


def test_whitespace_subject_rejected():
    v = make_verifier()
    assert verify(v, mint(base_claims(sub="   "))) is None


def test_signing_key_resolver_error_returns_none_and_logs(caplog):
    def boom(_token):
        raise jwt.PyJWKClientError("cannot reach JWKS")

    v = make_verifier(signing_key_resolver=boom)
    with caplog.at_level("WARNING", logger="context_layer.auth"):
        assert verify(v, mint(base_claims())) is None
    # A JWKS/infra failure must be surfaced, not silently swallowed as a bad token.
    assert any("resolve signing key" in r.message for r in caplog.records)


def test_jwks_url_shape():
    assert jwks_url_for("client_XYZ", "https://api.workos.com") == (
        "https://api.workos.com/sso/jwks/client_XYZ"
    )
    # trailing slash on base is tolerated
    assert jwks_url_for("c", "https://api.workos.com/") == "https://api.workos.com/sso/jwks/c"


def test_auth_kwargs_empty_when_workos_unconfigured(monkeypatch):
    monkeypatch.setattr("context_layer.config.WORKOS_CLIENT_ID", "")
    monkeypatch.setattr("context_layer.config.WORKOS_AUTHKIT_DOMAIN", "")
    monkeypatch.setattr("context_layer.config.PUBLIC_SERVER_URL", "")
    assert build_fastmcp_auth_kwargs() == {}


# --- resolve_user_id seam ---------------------------------------------------

def _set_principal(subject):
    """Populate the mcp auth contextvar as the request middleware would."""
    from mcp.server.auth.middleware.auth_context import (
        AuthenticatedUser,
        auth_context_var,
    )
    from mcp.server.auth.provider import AccessToken

    token = AccessToken(token="t", client_id="c", scopes=[], subject=subject)
    return auth_context_var.set(AuthenticatedUser(token))


def _reset_principal(reset):
    from mcp.server.auth.middleware.auth_context import auth_context_var

    auth_context_var.reset(reset)


def test_resolve_user_id_falls_back_to_default_without_auth():
    # No authenticated principal (stdio / capability-path / OAuth off).
    assert identity.resolve_user_id(None) == DEFAULT_USER_ID


def test_resolve_user_id_uses_prefixed_subject_when_authenticated(monkeypatch):
    monkeypatch.setattr(identity, "WORKOS_USER_ID_PREFIX", "workos_")
    reset = _set_principal("user_01ABC")
    try:
        assert identity.resolve_user_id(None) == "workos_user_01ABC"
    finally:
        _reset_principal(reset)


def test_resolve_user_id_default_when_subject_empty():
    reset = _set_principal(None)
    try:
        assert identity.resolve_user_id(None) == DEFAULT_USER_ID
    finally:
        _reset_principal(reset)


def test_resolve_user_id_default_when_subject_whitespace():
    # A whitespace-only subject must not produce a "workos_ " namespace.
    reset = _set_principal("   ")
    try:
        assert identity.resolve_user_id(None) == DEFAULT_USER_ID
    finally:
        _reset_principal(reset)
