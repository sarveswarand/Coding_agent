"""The PC assistant: spoken commands that act on this computer (open VS Code, open a project, open
Claude Code, switch project, list projects, report status). Anything not recognised here is a
prompt for the coding agent.

Anything that could lose work or be disruptive (closing an app, locking, shutting down) is not
done straight away: the action returns a Result with a `confirm`, the assistant asks, and only a
spoken "yes" within CONFIRM_TTL seconds runs it.

Each command is an Action in the ACTIONS list. To add one, write an async function that takes
(ctx, text) and returns a Result, or None if the sentence turned out not to be for it, and
register it with @action. Folders are only ever taken from the AGENT_WORKSPACES allowlist.
"""
import asyncio
import difflib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import apps as app_list
from . import local_llm
from .security import allowed_roots

SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__"}
UNSAFE_PATH = re.compile(r'[&|<>^%"\r\n]')  # code is a .cmd on Windows, so cmd.exe re-reads its arguments
GENERIC_WORDS = {"code", "project", "projects", "folder", "folders", "repo", "repos", "claude", "vscode",
                 "visual", "studio", "the", "this", "that", "app", "open", "please", "to", "in", "for"}
VSCODE = r"(?:v\s?s\s?code|vscode|visual studio code)"
CONFIRM_TTL = 60  # seconds a question stays open
YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|confirm|do it|go ahead|proceed)\b", re.I)
NO = re.compile(r"^\W*(no|nope|cancel|never ?mind|don't|do not|stop|abort)\b", re.I)


@dataclass
class Project:
    name: str   # the folder's name
    path: str

    @property
    def spoken(self) -> str:
        return re.sub(r"[_\-.]+", " ", self.name)


@dataclass
class Ctx:
    workspace: str          # the project the phone currently has selected
    session: object         # runs.Session: tells us whether the agent is busy and which folder it works in
    claude_session: str | None


@dataclass
class Confirm:
    """A risky action waiting for the user's "yes"."""
    do: Callable[[], Awaitable[str]]  # runs it and returns what to say
    made_at: float = field(default_factory=time.monotonic)


@dataclass
class Result:
    reply: str                      # what to say
    project: Project | None = None  # set when the active project changed
    confirm: Confirm | None = None  # set when the reply is a question that "yes" will carry out


# ---- projects -----------------------------------------------------------------------

def list_projects() -> list[Project]:
    """Each allowed root and the folders directly inside it."""
    out: list[Project] = []
    for r in allowed_roots():
        if not r.is_dir():
            continue
        out.append(Project(r.name or str(r), str(r)))
        try:
            kids = sorted(d for d in r.iterdir()
                          if d.is_dir() and not d.name.startswith(".") and d.name not in SKIP_DIRS)
        except OSError:
            kids = []
        out += [Project(d.name, str(d)) for d in kids[:200]]
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_project(text: str, projects: list[Project]) -> tuple[Project | None, list[Project]]:
    """The project named in `text`: (match, []) or (None, candidates) when it's ambiguous, or (None, []).
    Speech recognition spells names loosely ("coding agent" for Coding_Agent), so spaces, case and
    punctuation are ignored, and a close misspelling still counts."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    scored: list[tuple[float, Project]] = []
    for p in projects:
        n = _norm(p.name)
        if len(n) < 3 or n in GENERIC_WORDS:
            continue
        best = 0.0
        for size in (1, 2, 3, 4):
            for i in range(len(words) - size + 1):
                gram_words = words[i:i + size]
                if all(w in GENERIC_WORDS for w in gram_words):
                    continue
                gram = "".join(gram_words)
                if gram == n:
                    best = max(best, 1.0 + len(n) / 100)
                elif len(n) >= 5:
                    ratio = difflib.SequenceMatcher(None, gram, n).ratio()
                    if ratio >= 0.8:
                        best = max(best, ratio)
        if best:
            scored.append((best, p))
    if not scored:
        return None, []
    scored.sort(key=lambda t: -t[0])
    top = [p for s, p in scored if s >= scored[0][0] - 0.02]
    return (top[0], []) if len(top) == 1 else (None, top)


# ---- opening things -------------------------------------------------------------------

def _code_cli() -> str | None:
    found = shutil.which("code")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        p = Path(base) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
        if base and p.is_file():
            return str(p)
    return None


def _launch(args: list[str]) -> None:
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def open_vscode(path: str | None) -> str | None:
    """Open VS Code, on `path` if given. Returns an error sentence, or None on success."""
    code = _code_cli()
    if not code:
        return "I couldn't find VS Code on this PC."
    if path and UNSAFE_PATH.search(path):
        return "I can't open that folder because its name has special characters."
    _launch([code, path] if path else [code])
    return None


async def open_claude_panel(claude_session: str | None) -> str | None:
    code = _code_cli()
    if not code:
        return "I couldn't find VS Code on this PC."
    await asyncio.sleep(2.5)  # let the window come up before the panel is asked for
    _launch([code, "--open-url", "vscode://anthropic.claude-code/open"
             + (f"?session={claude_session}" if claude_session else "")])
    return None


def _busy_reply(ctx: Ctx) -> Result | None:
    return Result("The agent is busy. Say stop first, then try again.") if ctx.session.active else None


def _same_folder(a: str | None, b: str) -> bool:
    return bool(a) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---- the actions ----------------------------------------------------------------------

@dataclass
class Action:
    name: str
    pattern: re.Pattern
    max_words: int
    run: Callable[[Ctx, str], Awaitable[Result | None]]


ACTIONS: list[Action] = []


def action(name: str, pattern: str, max_words: int = 12):
    def register(fn):
        ACTIONS.append(Action(name, re.compile(pattern, re.I), max_words, fn))
        return fn
    return register


async def handle(text: str, ctx: Ctx) -> Result | None:
    """The first action that recognises the sentence and takes it, or None: it's a prompt for the agent."""
    waiting = getattr(ctx.session, "confirm", None)
    if waiting:
        ctx.session.confirm = None  # a question is answered once, or dropped by saying something else
        short = len(text.split()) <= 4
        if short and YES.search(text):
            if time.monotonic() - waiting.made_at > CONFIRM_TTL:
                return Result("That question timed out. Ask me again.")
            return Result(await waiting.do())
        if short and NO.search(text):
            return Result("Okay, cancelled.")
    n = len(text.split())
    for a in ACTIONS:
        if n <= a.max_words and a.pattern.search(text):
            result = await a.run(ctx, text)
            if result is not None:
                if result.confirm:
                    ctx.session.confirm = result.confirm
                return result
    return None


def _ambiguous(candidates: list[Project]) -> Result:
    return Result("Which one: " + " or ".join(c.spoken for c in candidates[:3]) + "?")


@action("list_projects", r"\b(list|show|read|which|what|tell me)\b.{0,25}\b(projects|folders|repos|repositories)\b", 10)
async def list_projects_action(ctx: Ctx, text: str) -> Result:
    names = [p.spoken for p in list_projects()]
    if not names:
        return Result("No projects are set up. Run the pair command with a workspace folder.")
    shown = names[:8]
    more = f", and {len(names) - 8} more" if len(names) > 8 else ""
    return Result(f"You have {len(names)} projects: " + ", ".join(shown) + more + ".")


@action("status", r"(what('s| is| are)?\s+(happening|going on|it doing|you doing|the status|the progress)|\bstatus\b|"
                  r"how('s| is) (it|that|everything) going|update me|any progress|are you (done|finished)|"
                  r"is it (done|finished)|where are you|what happened)", 10)
async def status_action(ctx: Ctx, text: str) -> Result:
    return Result(await status(ctx.session))


@action("open_claude", r"\b(open|launch|start|bring up|show)\b.{0,30}\bclaude\b", 12)
async def open_claude_action(ctx: Ctx, text: str) -> Result:
    target, candidates = find_project(text, list_projects())
    if candidates:
        return _ambiguous(candidates)
    path = target.path if target else ctx.workspace
    changed = target is not None and not _same_folder(target.path, ctx.workspace)
    if changed and (busy := _busy_reply(ctx)):
        return busy
    if err := open_vscode(path):
        return Result(err)
    session = ctx.claude_session if _same_folder(getattr(ctx.session, "workspace", None), path) else None
    if err := await open_claude_panel(session):
        return Result(err)
    return Result(f"Opened Claude Code in VS Code for {Path(path).name.replace('_', ' ')}.",
                  project=target if changed else None)


@action("open_vscode", rf"\b(open|launch|start|bring up)\b.{{0,40}}\b({VSCODE}|folder|project|repo|repository)\b"
                       r"|^\s*(open|launch)\b", 10)
async def open_vscode_action(ctx: Ctx, text: str) -> Result | None:
    target, candidates = find_project(text, list_projects())
    if candidates:
        return _ambiguous(candidates)
    mentions_editor = re.search(VSCODE, text, re.I)
    if target is None:
        if mentions_editor:
            return Result(open_vscode(ctx.workspace) or "Opened VS Code.")
        if re.search(r"\b(folder|project|repo|repository)\b", text, re.I):
            return Result("I couldn't find that folder. Say list my projects to hear the names.")
        return None  # "open the login page": not a folder, so it's a prompt for the agent
    changed = not _same_folder(target.path, ctx.workspace)
    if changed and (busy := _busy_reply(ctx)):
        return busy
    if err := open_vscode(target.path):
        return Result(err)
    return Result(f"Opened {target.spoken} in VS Code.", project=target if changed else None)


# ---- apps and the computer: opening is immediate, closing/locking/shutting down asks first ----

WINDOWS_ONLY = "That only works on Windows."
PC_WORDS = r"(pc|computer|laptop|machine|system)"


async def _run(args: list[str]) -> int:
    def go() -> int:
        return subprocess.run(args, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
    return await asyncio.to_thread(go)


@action("open_app", r"^\s*(open|launch|start|run)\b", 8)
async def open_app_action(ctx: Ctx, text: str) -> Result | None:
    app = app_list.find_app(text, app_list.load_apps())
    if app is None or not app.cmd:
        return None  # not an allowed app, so it falls through: it may be a prompt for the agent
    _launch(app.cmd)
    return Result(f"Opened {app.label}.")


@action("close_app", r"^\s*(close|quit|exit)\b", 8)
async def close_app_action(ctx: Ctx, text: str) -> Result | None:
    app = app_list.find_app(text, app_list.load_apps())
    if app is None:
        return None
    if not app.process:
        return Result(f"I don't close {app.label}.")
    if os.name != "nt":
        return Result(WINDOWS_ONLY)

    async def do() -> str:
        code = await _run(["taskkill", "/IM", app.process])  # no /F: the app may ask to save first
        return f"Closed {app.label}." if code == 0 else f"{app.label} isn't running."
    return Result(f"Close {app.label}? Say yes to confirm.", confirm=Confirm(do))


@action("lock", rf"\block\b.{{0,15}}\b{PC_WORDS}\b|\block (the )?screen\b", 6)
async def lock_action(ctx: Ctx, text: str) -> Result:
    if os.name != "nt":
        return Result(WINDOWS_ONLY)

    async def do() -> str:
        await _run(["rundll32.exe", "user32.dll,LockWorkStation"])
        return "Locked."
    return Result("Lock this PC? Say yes to confirm.", confirm=Confirm(do))


def _power_action(verb: str, flag: str, doing: str):
    async def run(ctx: Ctx, text: str) -> Result:
        if os.name != "nt":
            return Result(WINDOWS_ONLY)

        async def do() -> str:
            await _run(["shutdown", flag, "/t", "30"])
            return f"{doing} in 30 seconds. Say cancel shutdown to stop it."
        return Result(f"{verb} this PC? You won't be able to control it from here until it's back on. Say yes to confirm.",
                      confirm=Confirm(do))
    return run


action("shutdown", rf"\b(shut ?down|power off|turn off)\b.{{0,15}}\b{PC_WORDS}\b", 8)(_power_action("Shut down", "/s", "Shutting down"))
action("restart", rf"\b(restart|reboot)\b.{{0,15}}\b{PC_WORDS}\b", 8)(_power_action("Restart", "/r", "Restarting"))


@action("cancel_shutdown", r"\bcancel\b.{0,12}\b(shut ?down|restart|reboot)\b", 6)
async def cancel_shutdown_action(ctx: Ctx, text: str) -> Result:
    if os.name != "nt":
        return Result(WINDOWS_ONLY)
    code = await _run(["shutdown", "/a"])
    return Result("Shutdown cancelled." if code == 0 else "Nothing was scheduled to shut down.")



@action("switch_project", r"\b(switch|change|go|use|select|work|move|set)\b", 10)
async def switch_project_action(ctx: Ctx, text: str) -> Result | None:
    target, candidates = find_project(text, list_projects())
    if candidates:
        return _ambiguous(candidates)
    if target is None:
        return None  # "use axios instead": not about a project
    if _same_folder(target.path, ctx.workspace):
        return Result(f"Already on {target.spoken}.")
    if busy := _busy_reply(ctx):
        return busy
    return Result(f"Switched to {target.spoken}.", project=target)


# ---- "what's happening?" ----------------------------------------------------------------

def _facts(log: list[dict], active: bool) -> tuple[str, str | None]:
    """(a plain status sentence built from what really happened, the agent's last message or None).
    A small model made things up when asked to describe a run in progress, so this is not left to it."""
    steps = sum(1 for e in log if e["type"] == "tool_call")
    last = next((e for e in reversed(log) if e["type"] in ("tool_call", "approval_request", "done", "stopped", "error", "text")), None)
    if last is None:
        return "Nothing is running.", None
    t = last["type"]
    if t == "approval_request":
        return f"It is waiting for your approval: {last.get('speech') or 'a step needs your OK'}", None
    if active:
        doing = next((e.get("speech") for e in reversed(log) if e["type"] == "tool_call" and e.get("speech")), None)
        return (f"It is working. {steps} steps so far" + (f", and right now: {doing}" if doing else ".")), None
    if t == "error":
        return f"It stopped with an error: {last.get('message', '')[:150]}", None
    if t == "stopped":
        return "It was stopped.", None
    text = next((e["text"] for e in reversed(log) if e["type"] == "text"), "")
    return ("It is finished. Its last message was:" if text else "It is finished."), text


async def status(session) -> str:
    if not session.log:
        return "Nothing has happened in this conversation yet."
    sentence, last_text = _facts(session.log, session.active)
    if not last_text:
        return sentence
    last_text = re.sub(r"```[\s\S]*?```", " ", last_text).strip()
    if len(last_text) <= 300:
        return f"{sentence} {last_text}"
    short = await local_llm.ask(
        "Rewrite the message below as at most two short plain sentences for someone listening. "
        "Use only what it says. No code, no lists.", last_text[:2000])
    return f"{sentence} {short or last_text[:300]}"
