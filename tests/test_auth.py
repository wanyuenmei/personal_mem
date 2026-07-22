"""Unit tests for the M2.2 WorkOS OAuth resource-server wiring.

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
        user_id_prefix="workos_",
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
