import asyncio
import logging
import os
import platform
import re
import uuid
from pathlib import Path

from . import usage
from .gateway import RateLimited, default_model, get_provider
from .tools import READ_ONLY, TOOL_SCHEMAS, git_branch, preview, run_tool

log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn terminal

SYSTEM = ("You are OrgAI, a coding agent working inside the user's repository. "
          "Explore with tools before answering. Make minimal, correct edits. "
          "Never claim you ran something you didn't.")
INSTRUCTION_FILES = ("AGENTS.md", ".orgai.md")  # project notes, read from the workspace root
MAX_INSTRUCTIONS_CHARS = 20_000
# Shell syntax that could chain another command onto an allowed one.
SHELL_META = re.compile(r"[&|;<>`\r\n]|\$\(")
REJECTED = "User rejected this action."

pending: dict[str, asyncio.Future] = {}  # approval_id -> Future[(approved, always)]
sessions: dict[str, list] = {}           # session_id -> provider-neutral messages
always_allowed: dict[str, set] = {}      # session_id -> commands the user chose "Always allow" for


def _repair_history(msgs: list) -> None:
    """After a cancelled turn, leave the history valid for the next request:
    every tool call needs a result, and roles must make sense."""
    last = msgs[-1] if msgs else None
    if not last:
        return
    if last["role"] == "assistant" and last["tool_calls"]:
        msgs.append({"role": "tool", "results": [
            {"id": c["id"], "name": c["name"], "content": "Cancelled by user.", "is_error": True}
            for c in last["tool_calls"]]})
    elif last["role"] == "user":
        msgs.append({"role": "assistant", "text": "(Request interrupted)", "tool_calls": [], "raw": None})


def _system_prompt(workspace: str) -> str:
    """SYSTEM plus facts about this machine and the project's own instructions.
    Built once per request, so it stays identical across the loop (prompt cache)."""
    if os.name == "nt":
        shell = (f"{os.environ.get('COMSPEC', 'cmd.exe')}; use Windows commands (dir, type, findstr, set) "
                 'or powershell -NoProfile -Command "..."; ls, cat and grep may not exist')
    else:
        shell = "/bin/sh"
    env = [f"- OS: {platform.system()} {platform.release()}",
           f"- run_command shell: {shell}",
           f"- Workspace root: {workspace} (tool paths are relative to it)"]
    branch = git_branch(workspace)
    if branch:
        env.append(f"- Git branch: {branch}")
    parts = [SYSTEM, "Environment:\n" + "\n".join(env)]
    for name in INSTRUCTION_FILES:
        p = Path(workspace) / name
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")[:MAX_INSTRUCTIONS_CHARS]
            parts.append(f"Project instructions from {name} (written by the repository's maintainers):\n{text}")
    return "\n\n".join(parts)


def _with_context(text: str, ctx: dict | None) -> str:
    """Put what the user has open in the editor in front of their message."""
    if not ctx:
        return text
    lines = [f"Open file: {ctx['file']} (cursor on line {ctx['cursor_line']}"
             + ("; unsaved changes, so the file on disk may differ)" if ctx.get("unsaved") else ")")]
    if ctx.get("selection"):
        lines.append(f"Selected lines {ctx['selection_start']}-{ctx['selection_end']}:\n```\n{ctx['selection']}\n```")
    if ctx.get("problems"):
        lines.append("Problems VS Code reports in this file:")
        lines += [f"- line {p['line']} ({p['severity']}): {p['message']}" for p in ctx["problems"]]
    return "<editor_context>\n" + "\n".join(lines) + "\n</editor_context>\n\n" + text


def _auto_approval(session_id, name, args, approval_mode, allowed_commands) -> str | None:
    """Why this call may run without asking, or None to ask the user."""
    if name == "edit_file" and approval_mode == "autoEdits":
        return "file edits are auto-approved in settings"
    if name != "run_command":
        return None
    cmd = str(args.get("command", "")).strip()
    if cmd in always_allowed.get(session_id, ()):
        return "you chose Always allow for this command"
    if not cmd or SHELL_META.search(cmd):
        return None
    for allowed in (c.strip() for c in allowed_commands):
        if allowed and (cmd == allowed or cmd.startswith(allowed + " ")):
            return f"matches allowed command '{allowed}'"
    return None


async def run_agent(session_id, workspace, user_text, model=None, context=None,
                    approval_mode="ask", allowed_commands=()):
    msgs = sessions.setdefault(session_id, [])
    msgs.append({"role": "user", "text": _with_context(user_text, context)})
    model = model or default_model()
    provider = get_provider(model)
    aid = None
    try:
        system = await asyncio.to_thread(_system_prompt, workspace)
        for _ in range(25):  # hard cap on loop iterations
            for attempt in range(4):
                try:
                    turn = await provider.generate(model, system, msgs, TOOL_SCHEMAS)
                    break
                except RateLimited as e:  # e.g. Gemini free tier's requests-per-minute limit
                    if attempt == 3:
                        raise
                    wait = min(max(e.retry_after, 1.0), 90.0)
                    log.info("%s; retrying in %.0fs", e, wait)
                    yield {"type": "status", "text": f"Rate limit reached; retrying in {wait:.0f}s…"}
                    await asyncio.sleep(wait)
                    yield {"type": "status", "text": ""}
            msgs.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw})
            yield {"type": "usage", "model": model,
                   "session": usage.record(session_id, model, turn.usage)}
            if turn.text:
                yield {"type": "text", "text": turn.text}
            if not turn.tool_calls:
                break

            results = []
            for call in turn.tool_calls:
                name, args = call["name"], call["input"]
                yield {"type": "tool_call", "name": name, "input": args}
                result = {"id": call["id"], "name": name, "content": "", "is_error": False}
                try:
                    if name not in READ_ONLY:
                        shown = await asyncio.to_thread(preview, workspace, name, args)
                        why = _auto_approval(session_id, name, args, approval_mode, allowed_commands)
                        if why:
                            log.info("auto-approved %s: %s", name, why)
                            yield {"type": "auto_approved", "name": name, "preview": shown, "reason": why}
                        else:
                            aid = str(uuid.uuid4())
                            fut = asyncio.get_running_loop().create_future()
                            pending[aid] = fut
                            yield {"type": "approval_request", "id": aid, "name": name, "preview": shown}
                            log.info("waiting for approval of %s (%s)", name, aid)
                            approved, always = await fut
                            log.info("approval %s -> %s", aid, "approved" if approved else "rejected")
                            pending.pop(aid, None)
                            aid = None
                            if approved and always and name == "run_command":
                                always_allowed.setdefault(session_id, set()).add(str(args.get("command", "")).strip())
                            if not approved:
                                result.update(content=REJECTED, is_error=True)
                    if not result["is_error"]:
                        result["content"] = await asyncio.to_thread(run_tool, workspace, name, args)
                except Exception as e:
                    result.update(content=f"Error: {e}", is_error=True)
                results.append(result)
                if result["is_error"] and result["content"] != REJECTED:
                    log.warning("tool %s failed: %s", name, result["content"][:300])
                yield {"type": "tool_result", "name": name, "is_error": result["is_error"]}
            msgs.append({"role": "tool", "results": results})
        yield {"type": "done"}
    finally:
        # Runs on normal exit, on error, and when the turn is cancelled (Stop).
        if aid:
            pending.pop(aid, None)
        _repair_history(msgs)
