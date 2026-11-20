"""The voice assistant's routing: what the user said is either a command for the assistant itself
(open Claude Code, what's happening?) or a prompt for the coding agent."""
import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import local_llm

OPEN = re.compile(r"\b(open|launch|start|bring up|show)\b.{0,30}\bclaude\b", re.I)
STATUS = re.compile(
    r"(what('s| is| are)?\s+(happening|going on|it doing|you doing|the status|the progress)|\bstatus\b|"
    r"how('s| is) (it|that|everything) going|update me|any progress|are you (done|finished)|"
    r"is it (done|finished)|where are you|what happened)", re.I)
UNSAFE_PATH = re.compile(r'[&|<>^%"\r\n]')  # code is a .cmd on Windows, so cmd.exe re-reads its arguments


def classify(text: str) -> str:
    """'open', 'status' or 'prompt'. Only short utterances count as commands, so a long request
    that merely mentions Claude or a status page still goes to the agent."""
    words = len(text.split())
    if words <= 8 and OPEN.search(text):
        return "open"
    if words <= 10 and STATUS.search(text):
        return "status"
    return "prompt"


# ---- open Claude Code in VS Code ---------------------------------------------------

def _code_cli() -> str | None:
    found = shutil.which("code")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        p = Path(base) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
        if base and p.is_file():
            return str(p)
    return None


async def open_claude_code(workspace: str, claude_session: str | None) -> str:
    """Open the folder in VS Code and show the Claude Code panel, on the same conversation the
    voice agent is using when there is one."""
    code = _code_cli()
    if not code:
        return "I couldn't find VS Code on this PC."
    if UNSAFE_PATH.search(workspace):
        return "I can't open that folder because its name has special characters."
    url = "vscode://anthropic.claude-code/open" + (f"?session={claude_session}" if claude_session else "")
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
             "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    subprocess.Popen([code, workspace], **quiet)
    await asyncio.sleep(2.5)  # let the window come up before the panel is asked for
    subprocess.Popen([code, "--open-url", url], **quiet)
    return f"Opened Claude Code in VS Code for {Path(workspace).name}."


# ---- "what's happening?" -----------------------------------------------------------

def _digest(log: list[dict], limit: int = 30) -> list[str]:
    lines = []
    for ev in log[-limit:]:
        t = ev["type"]
        if t == "user":
            lines.append(f"The user asked: {ev['text'][:200]}")
        elif t == "steer":
            lines.append(f"The user added: {ev['text'][:200]}")
        elif t == "text":
            lines.append(f"The agent said: {ev['text'][:300]}")
        elif t == "tool_call":
            lines.append(f"The agent started: {ev.get('speech') or ev['name']}")
        elif t == "tool_result" and ev.get("is_error"):
            lines.append("A step failed.")
        elif t == "approval_request":
            lines.append(f"The agent is waiting for the user to approve: {ev.get('preview', '')[:120]}")
        elif t == "done":
            lines.append("The agent finished.")
        elif t == "stopped":
            lines.append("The user stopped the agent.")
        elif t == "error":
            lines.append(f"There was an error: {ev.get('message', '')[:150]}")
    return lines


def _plain_status(lines: list[str], active: bool) -> str:
    """Used when the local model isn't available: the latest fact, stated plainly."""
    last = lines[-1] if lines else ""
    if last.startswith("The agent is waiting"):
        return "It is waiting for your approval."
    if active:
        started = [l for l in lines if l.startswith("The agent started")]
        return (started[-1].replace("The agent started: ", "It is working on this: ") if started
                else "It is working on your request.")
    return last.replace("The agent said: ", "Last update: ")[:300] if last else "Nothing is running."


async def status(session) -> str:
    lines = _digest(session.log)
    if not lines:
        return "Nothing has happened in this conversation yet."
    reply = await local_llm.ask(
        "You are a voice assistant reporting on a coding agent. Using only the notes, say in at most two "
        "short plain sentences what the agent is doing right now, or what it just finished, and whether it "
        "needs the user. No code, no file contents, no lists.",
        "Notes (oldest first):\n" + "\n".join(lines) + f"\nThe agent is currently {'running' if session.active else 'idle'}.")
    return reply or _plain_status(lines, session.active)
