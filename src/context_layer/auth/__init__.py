"""Auth layer: capability-path + rate-limit guards and the WorkOS OAuth wiring.

Which mode is active is decided at composition time by config.workos_enabled():
WorkOS configured -> OAuth resource server (RateLimitGuard only); otherwise the
capability-path stopgap (CapabilityPathGuard).
"""

from context_layer.auth.guards import CapabilityPathGuard, RateLimitGuard
from context_layer.auth.workos import (
    WorkOSTokenVerifier,
    build_fastmcp_auth_kwargs,
    jwks_url_for,
)

__all__ = [
    "CapabilityPathGuard",
    "RateLimitGuard",
    "WorkOSTokenVerifier",
    "build_fastmcp_auth_kwargs",
    "jwks_url_for",
]
