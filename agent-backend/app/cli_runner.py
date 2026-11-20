"""Drive the Claude Code CLI as the agent, and translate what it does into the same events
run_agent produces, so the phone app (and runs.py) work with either.

Each run is one `claude -p` process in stream-json mode. Tool permission requests arrive on its
stdout as `can_use_tool` control requests; we answer them on stdin after the phone approves.
"""
import asyncio
import difflib
import glob
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from .orchestrator import NARRATE, REJECTED, _auto_approval, always_allowed, pending

log = logging.getLogger("uvicorn.error")

SESSION_FILE = Path(__file__).resolve().parent.parent / ".cli_sessions.json"
# These would make the CLI bill an API account / hand the remote token to a shell, so they are not passed on.
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AGENT_TOKEN")
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}


def find_claude() -> str | None:
    """CLAUDE_BIN, `claude` on PATH, or the binary bundled with the VS Code extension (newest)."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    exe = "claude.exe" if os.name == "nt" else "claude"
    pattern = str(Path.home() / ".vscode" / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary" / exe)
    found = sorted(glob.glob(pattern), key=lambda p: Path(p).stat().st_mtime)
    return found[-1] if found else None


def available() -> bool:
    return find_claude() is not None


# ---- sessions: our session id -> the CLI's, so a later message continues the same conversation ----

def _load_sessions() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def session_for(ours: str) -> str | None:
    """The Claude Code conversation id behind one of our sessions, if it has had a run."""
    return _load_sessions().get(ours)


def _save_session(ours: str, theirs: str) -> None:
    data = _load_sessions()
    data[ours] = theirs
    try:
        SESSION_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        log.warning("could not save %s", SESSION_FILE)


# ---- translating tools --------------------------------------------------------

def map_tool(name: str, inp: dict) -> tuple[str, dict]:
    """Claude Code tool -> the (name, input) shape the phone app and narrator know."""
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    if name == "Read":
        return "read_file", {"path": path}
    if name in EDIT_TOOLS:
        return "edit_file", {"path": path}
    if name in SHELL_TOOLS:
        return "run_command", {"command": str(inp.get("command", ""))}
    if name == "Grep":
        return "search", {"pattern": inp.get("pattern", "")}
    if name == "Glob":
        return "find_files", {"pattern": inp.get("pattern", "")}
    if name == "LS":
        return "list_dir", {"path": path or "."}
    return name, inp


def _diff(old: str, new: str, path: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), path, path))


def preview_for(name: str, inp: dict) -> str:
    """What the approval card shows."""
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if name == "Write":
        body = str(inp.get("content", ""))
        return (f"New file {path}\n" + "".join("+" + l for l in body.splitlines(True)))[:6000]
    if name == "Edit":
        return _diff(str(inp.get("old_string", "")), str(inp.get("new_string", "")), path)[:6000] or "(no changes)"
    if name == "MultiEdit":
        return "\n".join(_diff(str(e.get("old_string", "")), str(e.get("new_string", "")), path)
                         for e in inp.get("edits", []))[:6000]
    if name in SHELL_TOOLS:
        return str(inp.get("command", ""))
    return json.dumps(inp, indent=2, default=str)[:1500]


# ---- process handling -----------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    """End the CLI and anything it started (a running build or test, say)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def _start(cmd: list[str], cwd: str, loop, q: asyncio.Queue, stderr_tail: list[str]) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}  # never the API key: Claude Code uses its own login
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def pump_out():
        for line in proc.stdout:
            loop.call_soon_threadsafe(q.put_nowait, line)
        loop.call_soon_threadsafe(q.put_nowait, None)

    def pump_err():
        for line in proc.stderr:
            stderr_tail.append(line)
            del stderr_tail[:-20]

    threading.Thread(target=pump_out, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()
    return proc


def _send(proc: subprocess.Popen, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _user_msg(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


# ---- the run ----------------------------------------------------------------------

async def run_claude(session_id, workspace, user_text, model=None, context=None,
                     approval_mode="ask", allowed_commands=(), inbox: asyncio.Queue | None = None,
                     narrate=False):
    binary = find_claude()
    if not binary:
        raise RuntimeError("Claude Code isn't installed here. Install it, or set CLAUDE_BIN to its path.")
    cmd = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--permission-mode", "manual", "--permission-prompt-tool", "stdio"]
    resume = _load_sessions().get(session_id)
    if resume:
        cmd += ["--resume", resume]
    if narrate:
        cmd += ["--append-system-prompt", NARRATE]

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    stderr_tail: list[str] = []
    proc = await asyncio.to_thread(_start, cmd, workspace, loop, q, stderr_tail)
    tool_names: dict[str, str] = {}  # tool_use_id -> mapped name, to label the results
    turns, results, aid = 1, 0, None
    t_ev = asyncio.ensure_future(q.get())
    t_in = asyncio.ensure_future(inbox.get()) if inbox is not None else None
    try:
        _send(proc, _user_msg(user_text))
        while results < turns:
            done, _ = await asyncio.wait({t for t in (t_ev, t_in) if t}, return_when=asyncio.FIRST_COMPLETED)
            if t_in in done:  # the user spoke mid-run: queue it behind the current turn
                text = t_in.result()
                t_in = asyncio.ensure_future(inbox.get())
                _send(proc, _user_msg(text))
                turns += 1
                yield {"type": "steer", "text": text}
            if t_ev not in done:
                continue
            line = t_ev.result()
            if line is None:  # the process ended before finishing
                tail = "".join(stderr_tail).strip()[-500:]
                yield {"type": "error", "message": f"Claude Code stopped unexpectedly. {tail}".strip()}
                return
            t_ev = asyncio.ensure_future(q.get())
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")

            if kind == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
                _save_session(session_id, ev["session_id"])

            elif kind == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        yield {"type": "text", "text": block["text"]}
                    elif block.get("type") == "tool_use":
                        name, inp = map_tool(block["name"], block.get("input") or {})
                        tool_names[block["id"]] = name
                        yield {"type": "tool_call", "name": name, "input": inp}

            elif kind == "user":
                content = ev.get("message", {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_result":
                        yield {"type": "tool_result", "name": tool_names.get(block.get("tool_use_id"), "tool"),
                               "is_error": bool(block.get("is_error"))}

            elif kind == "control_request" and ev.get("request", {}).get("subtype") == "can_use_tool":
                req, rid = ev["request"], ev["request_id"]
                raw_name, inp = req["tool_name"], req.get("input") or {}
                name, mapped = map_tool(raw_name, inp)
                shown = preview_for(raw_name, inp)
                why = _auto_approval(session_id, name, mapped, approval_mode, allowed_commands)
                approved = True
                if why:
                    log.info("auto-approved %s: %s", raw_name, why)
                    yield {"type": "auto_approved", "name": name, "preview": shown, "reason": why}
                else:
                    aid = str(uuid.uuid4())
                    fut = loop.create_future()
                    pending[aid] = fut
                    yield {"type": "approval_request", "id": aid, "name": name, "preview": shown}
                    approved, always = await fut
                    pending.pop(aid, None)
                    aid = None
                    if approved and always and name == "run_command":
                        always_allowed.setdefault(session_id, set()).add(mapped["command"].strip())
                response = ({"behavior": "allow", "updatedInput": inp} if approved
                            else {"behavior": "deny", "message": REJECTED})
                _send(proc, {"type": "control_response",
                             "response": {"subtype": "success", "request_id": rid, "response": response}})

            elif kind == "result":
                results += 1
                if ev.get("is_error"):
                    yield {"type": "error", "message": str(ev.get("result") or "Claude Code reported an error.")}
        yield {"type": "done"}
    finally:
        if aid:
            pending.pop(aid, None)
        for t in (t_ev, t_in):
            if t:
                t.cancel()
        try:
            proc.stdin.close()
        except OSError:
            pass
        await asyncio.to_thread(_finish, proc, results < turns)


def _finish(proc: subprocess.Popen, interrupted: bool) -> None:
    """Let a finished CLI exit by itself; kill it (and its children) right away if the run was
    stopped or failed, or after a short grace period if it hangs."""
    if not interrupted:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    _kill_tree(proc)
