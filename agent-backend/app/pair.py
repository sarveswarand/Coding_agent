"""Set up remote access and print the link to open on your phone.

    python -m app.pair --workspace "C:\\Users\\you\\projects" [--url https://my-pc.tailnet.ts.net]

Creates AGENT_TOKEN in .env if there isn't one, records the allowed workspace folder(s), and prints
a link whose #fragment carries the token (a fragment is never sent to any server).
"""
import argparse
import os
import secrets
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"


def _set(lines: list[str], key: str, value: str) -> list[str]:
    out = [f"{key}={value}" if l.startswith(key + "=") else l for l in lines]
    return out if any(l.startswith(key + "=") for l in lines) else out + [f"{key}={value}"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", action="append", help="a folder the phone may work in (repeatable)")
    ap.add_argument("--url", default="http://localhost:8000", help="address your phone uses to reach this PC")
    ap.add_argument("--new-token", action="store_true", help="replace the existing token (unpairs old phones)")
    args = ap.parse_args()

    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    current = next((l.split("=", 1)[1] for l in lines if l.startswith("AGENT_TOKEN=")), "")
    token = current if current and not args.new_token else secrets.token_urlsafe(32)
    lines = _set(lines, "AGENT_TOKEN", token)
    if args.workspace:
        lines = _set(lines, "AGENT_WORKSPACES", os.pathsep.join(str(Path(w).resolve()) for w in args.workspace))
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Saved to {ENV}. Restart the backend, then open this on your phone:\n")
    print(f"  {args.url.rstrip('/')}/app/#token={token}\n")
    print("Anyone with this link can run commands on this PC. Keep it private.")


if __name__ == "__main__":
    main()
