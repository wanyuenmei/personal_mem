"""WorkOS AuthKit OAuth wiring for the MCP HTTP transport.

This makes the deployed server an OAuth 2.0 **Resource Server** (RFC 9728), not
an Authorization Server. Concretely:

  - FastMCP is constructed with ``auth=AuthSettings(...)`` + a ``token_verifier``.
    That makes FastMCP publish ``/.well-known/oauth-protected-resource`` (whose
    ``authorization_servers`` point at the WorkOS AuthKit issuer) and wrap /mcp
    in ``RequireAuthMiddleware``.
  - Discovery of the authorization-server metadata, **dynamic client
    registration (`/register`), `/authorize` and `/token` all live on WorkOS**,
    not here. The connector UI follows the protected-resource metadata to
    WorkOS, registers + signs the user in there, and returns to /mcp carrying a
    WorkOS-issued Bearer access token.
  - Our only job per request is to VERIFY that access token. WorkOS AuthKit
    access tokens are RS256 JWTs; we validate the signature against the tenant's
    public JWKS (``{base}/sso/jwks/{client_id}``), check issuer/expiry (and
    audience if configured), and expose the token's ``sub`` as the principal.

Everything here is dormant unless ``config.workos_enabled()`` — see config.py.

The verifier is deliberately structured around an injectable signing-key
resolver so its full validation path (signature, expiry, issuer, audience,
subject extraction, malformed input) is unit-testable with locally-minted
tokens and no network or real WorkOS credentials. See tests/test_auth.py.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from . import config

# A signing-key resolver maps a raw JWT to the key used to verify its signature.
# Production uses PyJWKClient (fetches + caches the tenant's public JWKS); tests
# inject a resolver returning a symmetric secret so HS256 tokens can be minted
# locally without any RSA/network machinery.
SigningKeyResolver = Callable[[str], Any]


def jwks_url_for(client_id: str, base_url: str) -> str:
    """WorkOS AuthKit JWKS URL for a client id (matches the WorkOS SDK)."""
    return f"{base_url.rstrip('/')}/sso/jwks/{client_id}"


def _pyjwk_resolver(jwks_url: str) -> SigningKeyResolver:
    """Default resolver: fetch + cache signing keys from a JWKS endpoint."""
    client = jwt.PyJWKClient(jwks_url)

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve


class WorkOSTokenVerifier(TokenVerifier):
    """Verify WorkOS AuthKit access tokens (RS256 JWTs) for the MCP RS.

    Implements the ``mcp`` ``TokenVerifier`` protocol: ``verify_token`` returns
    an ``AccessToken`` for a valid token or ``None`` for anything invalid
    (bad signature, expired, wrong issuer/audience, malformed, missing subject).
    Returning ``None`` — never raising — is what the protocol expects; the
    bearer-auth backend turns that into a 401.
    """

    def __init__(
        self,
        *,
        issuer: str,
        signing_key_resolver: SigningKeyResolver,
        audience: str | None = None,
        required_scopes: list[str] | None = None,
        algorithms: tuple[str, ...] = ("RS256",),
        user_id_prefix: str = "",
        leeway: int = 0,
    ) -> None:
        self.issuer = issuer.rstrip("/") if issuer else issuer
        self.audience = audience or None
        self.required_scopes = required_scopes or []
        self.algorithms = list(algorithms)
        self.user_id_prefix = user_id_prefix
        self.leeway = leeway
        self._resolve_key = signing_key_resolver

    @classmethod
    def from_config(cls) -> "WorkOSTokenVerifier":
        """Build a verifier from environment config (production path)."""
        jwks_url = jwks_url_for(config.WORKOS_CLIENT_ID, config.WORKOS_API_BASE_URL)
        return cls(
            issuer=config.WORKOS_AUTHKIT_DOMAIN,
            signing_key_resolver=_pyjwk_resolver(jwks_url),
            audience=config.WORKOS_AUDIENCE or None,
            required_scopes=config.workos_required_scopes(),
            algorithms=("RS256",),
            user_id_prefix=config.WORKOS_USER_ID_PREFIX,
        )

    @staticmethod
    def _extract_scopes(claims: dict[str, Any]) -> list[str]:
        # WorkOS/OAuth may present scopes as a space-delimited `scope` string or
        # a `scopes` list; tolerate either, default to none.
        raw = claims.get("scope") or claims.get("scopes") or []
        if isinstance(raw, str):
            return [s for s in raw.split() if s]
        if isinstance(raw, (list, tuple)):
            return [str(s) for s in raw]
        return []

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self._resolve_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=self.algorithms,
                issuer=self.issuer or None,
                audience=self.audience,
                leeway=self.leeway,
                # `verify_aud` is only meaningful when we actually know the
                # audience; WorkOS tenants that don't set `aud` would otherwise
                # fail decode.
                options={"verify_aud": self.audience is not None},
            )
        except Exception:
            # PyJWT raises subclasses of PyJWTError; PyJWKClient raises its own.
            # Any failure to produce verified claims is an invalid token.
            return None

        subject = claims.get("sub")
        if not subject:
            return None

        scopes = self._extract_scopes(claims)
        if self.required_scopes and not set(self.required_scopes).issubset(scopes):
            return None

        exp = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or self.issuer or subject),
            scopes=scopes,
            expires_at=int(exp) if exp is not None else None,
            subject=str(subject),
            claims=claims,
        )


def build_fastmcp_auth_kwargs() -> dict[str, Any]:
    """kwargs to pass to ``FastMCP(...)`` to turn on OAuth, or ``{}`` if off.

    Returns ``{}`` when WorkOS is not configured, so ``FastMCP`` is built exactly
    as before. When configured, returns ``auth=`` (an ``AuthSettings`` marking
    this an RFC 9728 resource server pointing at the AuthKit issuer) plus
    ``token_verifier=`` (our WorkOS JWT verifier).
    """
    if not config.workos_enabled():
        return {}
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(config.WORKOS_AUTHKIT_DOMAIN),
        resource_server_url=AnyHttpUrl(config.PUBLIC_SERVER_URL),
        required_scopes=config.workos_required_scopes() or None,
    )
    return {"auth": auth, "token_verifier": WorkOSTokenVerifier.from_config()}
