"""Who may talk to the agent, and which folders it may work in.

Remote clients (the phone app, under /v1) always need the bearer token AGENT_TOKEN and may only
use folders under AGENT_WORKSPACES. The older endpoints used by the VS Code extension keep working
without a token, but only for a direct local connection: a request that came through a proxy
such as `tailscale serve` shows up as loopback too, so any forwarding header counts as remote.
"""
import hmac
import os
from pathlib import Path

from fastapi import HTTPException, Request

PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "tailscale-user-login")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _token() -> str:
    return os.environ.get("AGENT_TOKEN", "").strip()


def allowed_roots() -> list[Path]:
    """Folders the agent may work in, from AGENT_WORKSPACES (separated by ';' on Windows, ':' elsewhere)."""
    raw = os.environ.get("AGENT_WORKSPACES", "")
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def _has_valid_token(request: Request) -> bool:
    token = _token()
    auth = request.headers.get("authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # compare_digest keeps the comparison time independent of how much of the token matched.
    return bool(token) and hmac.compare_digest(supplied.encode(), token.encode())


def is_direct_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in LOOPBACK and not any(h in request.headers for h in PROXY_HEADERS)


def require_token(request: Request) -> None:
    """Dependency for /v1: a valid token is always required."""
    if not _token():
        raise HTTPException(503, "Remote access is off: set AGENT_TOKEN (run `python -m app.pair`).")
    if not _has_valid_token(request):
        raise HTTPException(401, "Missing or invalid token.")


def require_local_or_token(request: Request) -> None:
    """Dependency for the VS Code endpoints: direct local connection, or a valid token."""
    if not (is_direct_local(request) or _has_valid_token(request)):
        raise HTTPException(401, "Missing or invalid token.")


def check_workspace(workspace: str, remote: bool) -> str:
    """Return the resolved workspace path, or raise 403 if it isn't an allowed folder.
    Remote requests need an allowlist; local ones are only restricted when one is set."""
    roots = allowed_roots()
    if remote and not roots:
        raise HTTPException(403, "No workspaces are allowed: set AGENT_WORKSPACES.")
    p = Path(workspace).expanduser().resolve()
    if roots and not any(p == r or p.is_relative_to(r) for r in roots):
        raise HTTPException(403, "That folder is outside the allowed workspaces.")
    if not p.is_dir():
        raise HTTPException(400, "Workspace folder does not exist.")
    return str(p)
