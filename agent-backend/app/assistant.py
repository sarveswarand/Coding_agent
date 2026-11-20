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
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import apps as app_list
from . import desktop, local_llm
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
    """An action waiting for the user's "yes"."""
    do: Callable[[], Awaitable["Result | str"]]  # runs it and returns what to say
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


CLAUDE_URL = "vscode://anthropic.claude-code/open"
MAX_URL_CHARS = 1800  # Windows hands longer addresses to programs unreliably


async def open_claude_panel(claude_session: str | None, prompt: str | None = None) -> str | None:
    """Show the Claude Code panel in VS Code. With `prompt`, a new conversation opens with that text in its
    input box; it is only placed there, never sent (the extension doesn't send it either)."""
    code = _code_cli()
    if not code:
        return "I couldn't find VS Code on this PC."
    params = []
    if prompt:
        params.append("prompt=" + urllib.parse.quote(prompt, safe=""))
    elif claude_session:
        params.append(f"session={claude_session}")
    url = CLAUDE_URL + ("?" + "&".join(params) if params else "")
    if len(url) > MAX_URL_CHARS:
        return "That's too long to put in the box in one go. Say it shorter."
    await asyncio.sleep(2.5)  # let the window come up before the panel is asked for
    if os.name == "nt":
        os.startfile(url)  # the vscode:// address goes straight to VS Code: no shell, no .cmd re-reading it
    else:
        _launch([code, "--open-url", url])
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


def action(name: str, pattern: str, max_words: int = 12, first: bool = False):
    """Register a command. `first=True` puts it ahead of the others, for commands that carry free text
    (like "tell Claude to fix the status page") which a keyword in that text must not hijack."""
    def register(fn):
        a = Action(name, re.compile(pattern, re.I), max_words, fn)
        ACTIONS.insert(0, a) if first else ACTIONS.append(a)
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
            done = await waiting.do()
            return done if isinstance(done, Result) else Result(done)
        if short and NO.search(text):
            return Result("Okay, cancelled.")
    n = len(text.split())
    result = None
    for a in ACTIONS:
        if n <= a.max_words and a.pattern.search(text):
            result = await a.run(ctx, text)
            if result is not None:
                break
    if result is None:
        result = await interpret(text, ctx)  # not a phrase we know: let the local model have a go
    if result is not None and result.confirm:
        ctx.session.confirm = result.confirm
    return result


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


SEND_TTL = 180  # seconds after filling the box during which "send it" is accepted
SEND_SUFFIX = re.compile(r"[\s,.]*(?:and\s+|then\s+)?(?:send|submit)(?:\s+(?:it|that))?\W*$|"
                         r"[\s,.]*(?:and\s+|then\s+)?(?:press|hit)\s+enter\W*$", re.I)
THERE_SUFFIX = re.compile(r"\s+(?:there|in there|in it|into it|in the box|in the chat)\W*$", re.I)
INSTRUCTION = re.compile(r"\b(?:and|then)\s+(?:type|ask|tell|say|write|prompt|enter)\s+(?:it\s+|claude\s+)?(?:to\s+)?(?P<q>.+)$", re.I)


META_LEAD = re.compile(r"^(?:you\s+|it\s+|claude\s+)?(?:to\s+)?open\b.*?\bclaude(?:\s+code)?\b\s*(?:and|then)\s+"
                       r"(?:type|enter|say|write|ask|tell|prompt)\s+(?:it\s+|claude\s+)?(?:to\s+)?(?P<q>.+)$", re.I)


def _clean_instruction(q: str) -> tuple[str, bool]:
    """(the text to put in Claude Code's box, whether the sentence also said to send it)."""
    meta = META_LEAD.match(q)  # "tell you to open Claude Code and type X": X is the instruction, the rest is how you asked
    if meta:
        q = meta.group("q")
    send = bool(SEND_SUFFIX.search(q))
    q = SEND_SUFFIX.sub("", q) if send else q
    return THERE_SUFFIX.sub("", q).strip(" .,"), send


async def _put_in_claude(ctx: Ctx, prompt: str, send: bool, target: Project | None = None) -> Result:
    """Open VS Code on the project, show Claude Code there with `prompt` in its box, and arm "send it".
    Nothing is sent unless the user says to (or said so in the same sentence)."""
    path = target.path if target else ctx.workspace
    changed = target is not None and not _same_folder(target.path, ctx.workspace)
    if changed and (busy := _busy_reply(ctx)):
        return busy
    if err := open_vscode(path):
        return Result(err)
    if err := await open_claude_panel(None, prompt=prompt):
        return Result(err)
    ctx.session.claude_armed = time.monotonic()
    if send:
        await asyncio.sleep(1.5)
        if desktop.supported() and desktop.foreground_program() == "code.exe":
            desktop.press("enter")
            ctx.session.claude_armed = None
            return Result("Put it in Claude Code in VS Code and sent it. Say what's on my screen to see how it's going.",
                          project=target if changed else None)
        return Result("Put it in Claude Code's box, but VS Code isn't in front, so I didn't send it. Say send it when it is.",
                      project=target if changed else None)
    return Result("Put it in Claude Code's box in VS Code. Say send it to submit, or what's on my screen to check it first.",
                  project=target if changed else None)


@action("open_claude", r"\b(open|launch|start|bring up|show)\b.{0,30}\bclaude\b", 40, first=True)
async def open_claude_action(ctx: Ctx, text: str) -> Result:
    """"open Claude Code", or "open Claude Code and type fix the login bug" (the text is placed in its box)."""
    m = INSTRUCTION.search(text)
    head = text[:m.start()] if m else text  # a project name inside the instruction is part of the instruction
    target, candidates = find_project(head, list_projects())
    if candidates:
        return _ambiguous(candidates)
    if m:
        prompt, send = _clean_instruction(m.group("q"))
        if prompt:
            return await _put_in_claude(ctx, prompt, send, target)
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


CLAUDE_TELL = [
    re.compile(r"^\W*(?:please\s+)?(?:tell|ask|prompt|have)\s+claude(?:\s+code)?\s+(?:to\s+)?(?P<q>.+)$", re.I),
    re.compile(r"^\W*(?:please\s+)?claude(?:\s+code)?\s*[,:]\s*(?P<q>.+)$", re.I),
    re.compile(r"^\W*(?:please\s+)?type\s+(?P<q>.+?)\s+(?:in|into|on)\s+(?:the\s+)?claude(?:\s+code)?"
               r"(?:\s+(?:box|input|chat|panel))?\W*$", re.I),
]


@action("claude_prompt", r"^\W*(?:please\s+)?(tell|ask|prompt|have|claude|type)\b", 60, first=True)
async def claude_prompt_action(ctx: Ctx, text: str) -> Result | None:
    """"tell Claude to fix the login bug", "ask Claude why the build fails", "type X in Claude Code".
    The only way text reaches Claude Code: it goes into the box in VS Code, and you say when to send it."""
    for rx in CLAUDE_TELL:
        m = rx.match(text)
        if m:
            prompt, send = _clean_instruction(m.group("q"))
            return await _put_in_claude(ctx, prompt, send) if prompt else None
    return None


@action("send_it", r"^\W*(?:ok(?:ay)?\s+|now\s+)?(send|submit)\b(?:\s+(?:it|that|this|the prompt|the message))?\W*$", 6, first=True)
async def send_it_action(ctx: Ctx, text: str) -> Result:
    armed = getattr(ctx.session, "claude_armed", None)
    if not armed or time.monotonic() - armed > SEND_TTL:
        return Result("There's nothing waiting to send. Say tell Claude to, and what you want, first.")
    if not desktop.supported():
        return Result(WINDOWS_ONLY)
    if desktop.foreground_program() != "code.exe":
        return Result("VS Code isn't in front, so I won't press enter. Bring it to the front and say send it again.")
    desktop.press("enter")
    ctx.session.claude_armed = None
    return Result("Sent. Say what's on my screen to see how it's going.")


@action("open_vscode", rf"\b(open|launch|start|bring up)\b.{{0,40}}\b({VSCODE}|folder|project|repo|repository)\b"
                       r"|^\s*(open|launch)\b", 10)
async def open_vscode_action(ctx: Ctx, text: str) -> Result | None:
    if DOMAIN.search(text.lower().replace(" dot ", ".")):
        return None  # "open github dot com" is a website, even if a project folder is called Github
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


# ---- opening websites (in the browser, not through the agent) -------------------------------------

SITES_FILE = Path(__file__).resolve().parent.parent / "sites.json"
# (spoken names, label, address). "inbox" and "my email" mean Gmail unless sites.json says otherwise.
DEFAULT_SITES = [
    (("gmail", "inbox", "my email", "my mail"), "Gmail", "https://mail.google.com/mail/u/0/#inbox"),
    (("youtube",), "YouTube", "https://www.youtube.com/"),
    (("google calendar", "calendar"), "Google Calendar", "https://calendar.google.com/"),
    (("google drive", "drive"), "Google Drive", "https://drive.google.com/"),
    (("google maps", "maps"), "Google Maps", "https://www.google.com/maps"),
    (("linkedin",), "LinkedIn", "https://www.linkedin.com/"),
    (("whatsapp web", "whatsapp"), "WhatsApp Web", "https://web.whatsapp.com/"),
    (("stack overflow", "stackoverflow"), "Stack Overflow", "https://stackoverflow.com/"),
]
DOMAIN = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|in|dev|ai|app|edu|gov|co)\b(?:/[^\s]*)?")
SAME_TAB = re.compile(r"\b(same|this|current|existing)\s+tab\b", re.I)
SITE_REQUEST = r"^\W*(?:please\s+)?(open|go to|pull up|bring up|launch|take me to|navigate to)\b"


def _sites() -> list[tuple[tuple[str, ...], str, str]]:
    """sites.json entries first (so they win), then the defaults.
    Format: {"sites": {"outlook": {"label": "Outlook", "url": "https://outlook.office.com/mail/", "names": ["outlook", "inbox"]}}}"""
    mine = []
    try:
        for key, spec in json.loads(SITES_FILE.read_text(encoding="utf-8")).get("sites", {}).items():
            url = str(spec.get("url", ""))
            if url.startswith(("https://", "http://")):
                mine.append((tuple(n.lower() for n in spec.get("names", [key])), spec.get("label", key), url))
    except (OSError, ValueError, AttributeError):
        pass
    return mine + DEFAULT_SITES


def _find_site(low: str) -> tuple[str, str] | None:
    """(label, address) of the site named in the (lower-case) sentence: the longest spoken name wins,
    or a plain domain like 'github.com' ("github dot com" is understood too)."""
    best: tuple[int, str, str] | None = None
    for names, label, url in _sites():
        for n in names:
            if re.search(rf"\b{re.escape(n)}\b", low) and (best is None or len(n) > best[0]):
                best = (len(n), label, url)
    if best:
        return best[1], best[2]
    m = DOMAIN.search(low)
    return (m.group(0).split("/")[0], "https://" + m.group(0)) if m else None


@action("open_site", SITE_REQUEST, 12)
async def open_site_action(ctx: Ctx, text: str) -> Result | None:
    """"open my gmail inbox in chrome", "go to youtube", "open github dot com". Claude Code can't drive a
    browser, so this opens the address itself: in a new Chrome tab, or in the tab in front if you say
    "in the same tab" (it types the address into that tab's address bar)."""
    low = text.lower().replace(" dot ", ".")
    if CODE_WORDS.search(low) and not SAID_WEB.search(low):
        return None  # "open the drive folder", "open the maps component": about the project
    site = _find_site(low)
    if site is None:
        return None
    label, url = site
    if SAME_TAB.search(text):
        if desktop.supported() and desktop.foreground_program() in ("chrome.exe", "msedge.exe"):
            desktop.hotkey("ctrl", "l")
            await asyncio.sleep(0.3)
            desktop.type_text(url)
            await asyncio.sleep(0.2)
            desktop.press("enter")
            return Result(f"Opened {label} in this tab.")
        note = " Your browser isn't in front, so it's a new tab."
    else:
        note = ""
    apps = app_list.load_apps()
    browser = apps.get("edge") if re.search(r"\bedge\b", low) else apps.get("chrome")
    if browser and browser.cmd:
        _launch([*browser.cmd, url])  # a program path and one address: no shell involved
        where = browser.label
    else:
        webbrowser.open(url)
        where = "your browser"
    return Result(f"Opened {label} in {where}.{note}")


def _leftover_words(text: str, app) -> list[str]:
    """Words in the request that are neither filler nor part of the app's name: "open my inbox in chrome"
    has "inbox" left over, so it isn't just "open Chrome"."""
    known = set(app_list.FILLER) | {"in", "on", "at", "with", "window", "new", "tab"}
    for n in app.names():
        known |= set(re.findall(r"[a-z0-9]+", n.lower()))
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in known]


@action("open_app", r"^\s*(open|launch|start|run)\b", 8)
async def open_app_action(ctx: Ctx, text: str) -> Result | None:
    app = app_list.find_app(text, app_list.load_apps())
    if app is not None and app.cmd:
        if _leftover_words(text, app):
            return None  # it asks for more than the app itself; don't quietly do only part of it
        _launch(app.cmd)
        return Result(f"Opened {app.label}.")
    # Not on the short list: anything with a Start Menu shortcut can still be opened by name ("open MySQL
    # Workbench"), but only with open/launch and only when the whole request is that app's name.
    if re.match(r"^\W*(open|launch)\b", text, re.I) and os.name == "nt":
        installed = app_list.find_installed(text)
        if installed:
            name, shortcut = installed
            os.startfile(shortcut)  # exactly what double-clicking the shortcut does: no arguments, no shell
            return Result(f"Opened {name}.")
    return None  # not an app we know: it may be a prompt for the agent


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
    return Result("Lock this PC? I can't unlock it from your phone, so you'd have to be at it. Say yes to confirm.",
                  confirm=Confirm(do))


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


# ---- searching the web in the browser ----------------------------------------------------------

SEARCH_LEAD = re.compile(
    r"^\W*(?:please\s+)?(?:search|google|look\s*up)\b"
    r"(?:\s+(?:(?:on|in)\s+)?(?:google|the\s+web|the\s+internet|online|chrome|edge|the\s+browser))?"
    r"(?:\s+for)?\s+(?P<q>.+?)\W*$", re.I)
SEARCH_TAIL = re.compile(r"\s+(?:on|in|using|with)\s+(?:google|chrome|edge|the\s+web|the\s+internet|the\s+browser|online)\W*$", re.I)
SAID_WEB = re.compile(r"\b(google|the web|internet|online|chrome|edge|browser)\b", re.I)
CODE_WORDS = re.compile(r"\b(code|codebase|repo|repository|project|file|files|folder|function|class|method|variable|"
                        r"component|endpoint|route|module|import|error|errors|bug|bugs|test|tests|todo|usages?|references|"
                        r"where|how|why|defined|used|implemented)\b", re.I)
CODE_SHAPE = re.compile(r"[_/\\]|\w\.\w|[a-z][A-Z]")  # snake_case, paths, dotted names, camelCase
BARE_SEARCH_MAX_WORDS = 4  # "search for X" with nothing about the web: only short, non-code-looking X goes to the browser


@action("web_search", r"^\W*(?:please\s+)?(search|google|look\s*up)\b", 14)
async def web_search_action(ctx: Ctx, text: str) -> Result | None:
    """Search Google in the browser. "search for Sundar Pichai" or "google X" does; a request that
    looks about the code ("search for where the token is validated") stays a prompt for the agent,
    unless the sentence says google/the web/chrome."""
    m = SEARCH_LEAD.match(text)
    query = SEARCH_TAIL.sub("", m.group("q")).strip() if m else ""
    if not query:
        return None
    if not SAID_WEB.search(text) and (len(query.split()) > BARE_SEARCH_MAX_WORDS
                                       or CODE_WORDS.search(query) or CODE_SHAPE.search(query)):
        return None
    apps = app_list.load_apps()
    browser = apps.get("edge") if re.search(r"\bedge\b", text, re.I) else apps.get("chrome")
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    if browser and browser.cmd:
        _launch([*browser.cmd, url])  # a program path and one URL argument: no shell involved
        where = browser.label
    else:
        webbrowser.open(url)
        where = "your browser"
    return Result(f"Searching Google for {query} in {where}.")


WEB_TOPIC = re.compile(r"\b(weather|forecast|temperature|news|score|scores|stock price|exchange rate|time in)\b", re.I)
QUESTION_LEAD = re.compile(r"^\W*(?:please\s+)?(?:(?:check|show|tell|give|get)(?:\s+me)?|what(?:'s|\s+is)|whats|how(?:'s|\s+is))\s+(?:the\s+)?", re.I)


@action("web_question", r"^\W*(?:please\s+)?(check|show|tell|give|get|what|whats|how)\b", 10)
async def web_question_action(ctx: Ctx, text: str) -> Result | None:
    """"check the Chennai weather", "what's the IPL score": a question the web answers, so it opens in the browser."""
    query = QUESTION_LEAD.sub("", text).strip(" ?.!")  # without "how's", "what is"...: those words aren't about code here
    if not query or not WEB_TOPIC.search(query) or CODE_WORDS.search(query) or CODE_SHAPE.search(query):
        return None
    return await web_search_action(ctx, f"search for {query} on google")


# ---- typing and keys, only into a browser or Notepad ----------------------------------------------

TYPE_LEAD = re.compile(r"^\W*(?:please\s+)?type\s+(?P<s>.+?)\W*$", re.I)
KEY_PRESS = re.compile(r"^\W*(?:please\s+)?(?:press|hit)\s+(?:the\s+)?(?P<k>enter|tab|escape|backspace)(?:\s+key)?\W*$", re.I)


TYPE_CODEISH = re.compile(r"\b(check|checking|annotations?|hints?|hinting|safe|script|alias|def|declarations?)\b", re.I)


def _typing_target() -> tuple[str | None, str | None]:
    """(program in front, None) if typing there is allowed, else (program or None, reply explaining why not)."""
    if not desktop.supported():
        return None, WINDOWS_ONLY
    front = desktop.foreground_program()
    if front in desktop.type_targets():
        return front, None
    return front, (f"The window in front is {front or 'unknown'}, so I won't type there. "
                   "Open Chrome or Notepad first.")


@action("type_text", r"^\W*(?:please\s+)?type\b", 14)
async def type_text_action(ctx: Ctx, text: str) -> Result | None:
    m = TYPE_LEAD.match(text)
    if not m:
        return None
    rest = m.group("s")
    front, why_not = _typing_target()
    if why_not:
        # "type check the project" or "type annotations for this function" is an instruction for the agent,
        # not keystrokes: only a short, plain phrase gets the "I won't type there" explanation.
        if TYPE_CODEISH.search(rest) or CODE_WORDS.search(rest) or CODE_SHAPE.search(rest) or len(rest.split()) > 6:
            return None
        return Result(why_not)
    desktop.type_text(rest)
    return Result(f"Typed it into {front}. Say press enter to submit.")


@action("press_key", r"^\W*(?:please\s+)?(press|hit)\b", 6)
async def press_key_action(ctx: Ctx, text: str) -> Result | None:
    m = KEY_PRESS.match(text)
    if not m:
        return None
    front, why_not = _typing_target()
    if why_not:
        return Result(why_not)
    desktop.press(m.group("k").lower())
    return Result(f"Pressed {m.group('k').lower()}.")


# ---- looking at the screen (the picture stays on this PC: a local vision model reads it) ----------

SCREEN_LOOK = (r"what('s| is)?\s+(on|in)\s+(my|the)\s+(screen|display|monitor)|"
               r"\b(look at|see|read|describe|check)\s+(my|the)\s+screen|what\s+(do|can)\s+you\s+see|"
               r"what\s+am\s+i\s+(looking at|seeing)")


@action("look_screen", SCREEN_LOOK, 10)
async def look_screen_action(ctx: Ctx, text: str) -> Result:
    if not desktop.supported():
        return Result(WINDOWS_ONLY)
    if local_llm.vision_model_name() is None:
        return Result("I don't have a vision model yet. On the PC, run: ollama pull qwen2.5vl:3b")

    async def work() -> str:
        png = await asyncio.to_thread(desktop.screenshot_png)
        # No hints about what should be there: a small model repeats them as if it had read them off the screen.
        seen = await local_llm.describe_image(
            png, "This is a screenshot of a computer screen. In at most three short plain sentences, say which "
                 "app or website is in front and what it shows, quoting any important text exactly as you read it. "
                 "Only describe what you can actually see.")
        return seen or "I couldn't read the screen. The vision model didn't answer in time."

    ctx.session.spawn(work())  # slow on a CPU: answer right away, speak the description when it's ready
    return Result("Looking at your screen. This takes a moment.")


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


# ---- loosely worded requests: the local model suggests, the user confirms -----------------------

COMMAND_WORDS = re.compile(
    r"\b(open|launch|start|pull up|bring up|fire up|boot|load|show me|take me|go to|get me|switch|jump|use|"
    r"close|quit|exit|kill|get rid of|shut|lock|up to|how far|progress|doing|done yet)\b", re.I)
ROUTE_MAX_WORDS = 10
ROUTE_ACTIONS = ["open_app", "close_app", "open_project", "switch_project", "open_claude", "open_vscode",
                 "lock", "list_projects", "status", "prompt"]
ROUTE_SCHEMA = {"type": "object",
                "properties": {"action": {"type": "string", "enum": ROUTE_ACTIONS}, "target": {"type": "string"}},
                "required": ["action", "target"]}


def _route_prompt(apps: dict, projects: list[Project]) -> str:
    app_names = [a.label for a in apps.values() if a.cmd or a.process]
    first_app = app_names[0] if app_names else "Notepad"
    first_proj = projects[0].name if projects else "MyProject"
    return (
        'You route one spoken request for a PC assistant. Reply with JSON {"action": ..., "target": ...}.\n'
        "Actions:\n"
        "- open_app: open one of the apps (target = the app's name)\n"
        "- close_app: close one of the apps (target = the app's name)\n"
        "- open_project: open a project folder in VS Code (target = the project's name)\n"
        "- switch_project: make a project the one being worked on (target = the project's name)\n"
        "- open_claude: show the Claude Code panel (target = a project's name, or empty)\n"
        "- open_vscode: open VS Code (target empty)\n"
        "- lock: lock the PC (target empty)\n"
        "- list_projects: read out the project names (target empty)\n"
        "- status: say what the coding agent is doing now (target empty)\n"
        "- prompt: anything else, especially anything about writing, changing, fixing, running, testing or "
        "explaining code. When unsure, use prompt.\n"
        f"Apps: {', '.join(app_names)}\n"
        f"Projects: {', '.join(p.name for p in projects[:60])}\n"
        "The target must be copied exactly from those lists, or be empty.\n"
        "Examples:\n"
        f'"fire up {first_app}" -> {{"action":"open_app","target":"{first_app}"}}\n'
        f'"get rid of {first_app}" -> {{"action":"close_app","target":"{first_app}"}}\n'
        f'"take me to {first_proj}" -> {{"action":"open_project","target":"{first_proj}"}}\n'
        '"fix the login bug" -> {"action":"prompt","target":""}\n'
        '"show me what you changed" -> {"action":"prompt","target":""}')


async def interpret(text: str, ctx: Ctx) -> Result | None:
    """For a short sentence no pattern recognised: ask the local model which action it means.
    Whatever it picks is checked against the real app and project lists, and the user is asked
    "Did you mean ...?" before anything happens. Anything doubtful stays a prompt for the agent."""
    if len(text.split()) > ROUTE_MAX_WORDS:
        return None
    apps, projects = app_list.load_apps(), list_projects()
    if not (COMMAND_WORDS.search(text) or app_list.find_app(text, apps) or find_project(text, projects)[0]):
        return None  # nothing command-like: don't spend a model call (or a delay) on an ordinary prompt
    raw = await local_llm.ask(_route_prompt(apps, projects), text, max_tokens=60, timeout=12, schema=ROUTE_SCHEMA)
    if not raw:
        return None
    try:
        action = json.loads(raw)["action"]
    except (ValueError, KeyError, TypeError):
        return None
    return await _dispatch(action, text, ctx, apps, projects)


async def _dispatch(action: str, text: str, ctx: Ctx, apps: dict, projects: list[Project]) -> Result | None:
    """Turn the model's suggestion into an action, but only where the user's own words back it up.
    A small model will happily pick some real-looking app or folder for "open the login page"."""
    if action not in ROUTE_ACTIONS or action == "prompt":
        return None
    said_app = app_list.find_app(text, apps)      # an app the user actually named (or an alias of it)
    said_proj, _ = find_project(text, projects)   # a project the user actually named
    if said_app and said_app.cmd and action in ("open_app", "open_vscode", "open_project", "switch_project"):
        action = "open_app"  # "fire up the text editor": they said Notepad, whatever the model picked
    app = said_app if action in ("open_app", "close_app") else None
    proj = said_proj if action in ("open_project", "switch_project", "open_claude") else None
    if action == "open_vscode" and not re.search(VSCODE, text, re.I):
        return None
    if action == "open_claude" and "claude" not in text.lower():
        return None
    if action == "lock" and not re.search(r"\block|screen", text, re.I):
        return None

    # harmless to read out, or already asks for its own confirmation
    if action == "list_projects":
        return await list_projects_action(ctx, "")
    if action == "status":
        return await status_action(ctx, "")
    if action == "lock":
        return await lock_action(ctx, "")
    if action == "close_app":
        return await close_app_action(ctx, f"close {app.label}") if app else None

    # everything else: say what was understood and wait for "yes"
    if action == "open_app" and app and app.cmd:
        what, run = f"open {app.label}", lambda: open_app_action(ctx, f"open {app.label}")
    elif action == "open_project" and proj:
        what, run = f"open the {proj.spoken} folder in VS Code", lambda: open_vscode_action(ctx, f"open the {proj.name} folder")
    elif action == "switch_project" and proj:
        what, run = f"switch to {proj.spoken}", lambda: switch_project_action(ctx, f"switch to {proj.name}")
    elif action == "open_claude":
        where = f" in {proj.name}" if proj else ""
        what = "open Claude Code" + (f" for {proj.spoken}" if proj else "")
        run = lambda: open_claude_action(ctx, f"open claude code{where}")
    elif action == "open_vscode":
        what, run = "open VS Code", lambda: open_vscode_action(ctx, "open vs code")
    else:
        return None  # the model named something that isn't on the lists

    async def do() -> Result:
        return (await run()) or Result("That didn't work.")
    return Result(f"Did you mean: {what}? Say yes.", confirm=Confirm(do))


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
