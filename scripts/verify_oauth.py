"""Pre-flight checks for WorkOS OAuth mode against a LIVE deploy.

Proves the RFC 9728 resource-server wiring without any credentials: health,
protected-resource discovery, and the unauthenticated-401 that makes connector
UIs launch WorkOS sign-in. It does NOT prove a real token verifies — only the
Claude/ChatGPT browser flows do that (see docs/workos-oauth-runbook.md).

Run:  python scripts/verify_oauth.py https://<your-domain>
Optionally assert the discovered values match what you configured:
      python scripts/verify_oauth.py https://<your-domain> \
          --issuer https://<app>.authkit.app --resource https://<your-domain>/mcp

Stdlib only, so it runs anywhere that can reach the deploy. Exit code 0 = all
checks passed, 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 10


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    """GET a URL, returning (status, headers, body) even for error statuses.

    An unreachable host (DNS failure, timeout, connection refused) raises the
    parent URLError rather than HTTPError; return a sentinel status of 0 with the
    reason in the body so callers report a clean FAIL instead of aborting the run.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except urllib.error.URLError as e:
        return 0, {}, f"unreachable: {e.reason}".encode()


def _post_no_auth(url: str) -> tuple[int, dict[str, str]]:
    """POST with no Authorization header; return (status, headers)."""
    # A minimal JSON-RPC-ish body; content doesn't matter — auth is checked first.
    req = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
    except urllib.error.URLError:
        # Unreachable host raises the parent URLError; a sentinel status of 0
        # lets the caller report a clean FAIL instead of crashing the run.
        return 0, {}


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))

    def fail(self, label: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def check_health(base: str, c: Checks) -> None:
    status, _, body = _get(f"{base}/health")
    if status == 200 and body.strip() == b"ok":
        c.ok("GET /health", "200 ok")
    else:
        c.fail("GET /health", f"got {status} {body[:40]!r} (is the server up?)")


def check_discovery(base: str, c: Checks, issuer: str | None, resource: str | None) -> None:
    # FastMCP serves protected-resource metadata at the root well-known path and
    # a path-suffixed variant; a green result on either is enough.
    paths = [
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ]
    meta = None
    for p in paths:
        status, _, body = _get(f"{base}{p}")
        if status == 200:
            try:
                meta = json.loads(body)
            except ValueError:
                c.fail(f"GET {p}", "200 but body is not JSON")
                continue
            c.ok(f"GET {p}", "200 JSON")
            break
    if meta is None:
        c.fail("protected-resource discovery", "no well-known metadata returned 200 JSON")
        return

    servers = meta.get("authorization_servers") or []
    got_resource = meta.get("resource")
    print(f"        authorization_servers={servers} resource={got_resource!r}")

    if servers:
        c.ok("metadata.authorization_servers present", str(servers))
        if issuer and not any(issuer.rstrip("/") == s.rstrip("/") for s in servers):
            c.fail("issuer match", f"expected {issuer!r} in {servers}")
        elif issuer:
            c.ok("issuer match", issuer)
    else:
        c.fail("metadata.authorization_servers present", "empty — connectors can't find WorkOS")

    if resource and got_resource:
        if resource.rstrip("/") == str(got_resource).rstrip("/"):
            c.ok("resource match", resource)
        else:
            c.fail("resource match", f"expected {resource!r}, got {got_resource!r}")


def check_unauthenticated_401(base: str, c: Checks) -> None:
    status, headers = _post_no_auth(f"{base}/mcp")
    www = next((v for k, v in headers.items() if k.lower() == "www-authenticate"), "")
    if status == 401:
        if "resource_metadata" in www.lower():
            c.ok("POST /mcp (no token)", "401 with WWW-Authenticate resource_metadata")
        else:
            c.fail("POST /mcp (no token)",
                   f"401 but WWW-Authenticate lacks resource_metadata: {www!r}")
    elif status == 404:
        c.fail("POST /mcp (no token)",
               "404 — server is in capability-path mode; WorkOS env not fully set")
    else:
        c.fail("POST /mcp (no token)", f"expected 401, got {status}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify WorkOS OAuth resource-server wiring on a live deploy.")
    ap.add_argument("base_url", help="Public origin of the deploy, e.g. https://your-domain")
    ap.add_argument("--issuer", help="Expected WORKOS_AUTHKIT_DOMAIN, asserted against metadata")
    ap.add_argument("--resource", help="Expected PUBLIC_SERVER_URL, asserted against metadata")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    print(f"Verifying OAuth wiring at {base}\n")

    c = Checks()
    check_health(base, c)
    check_discovery(base, c, args.issuer, args.resource)
    check_unauthenticated_401(base, c)

    print()
    if c.failed:
        print(f"{c.failed} check(s) FAILED — not ready for the connector-UI flows.")
        return 1
    print("All wiring checks passed. Next: run the Claude + ChatGPT connector flows "
          "in docs/workos-oauth-runbook.md (only those prove a real token verifies).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
