"""The apps the voice assistant may open and close.

Two tiers. The short allowlist (the common Windows apps found on this PC, plus anything in
agent-backend/apps.json, see apps.example.json) can be opened and closed. Beyond that, "open X" can
start anything with a Start Menu shortcut, by name only: the shortcut is launched exactly as
double-clicking it would, with no arguments and no shell, and these apps are never closed from here.
"""
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

APPS_FILE = Path(__file__).resolve().parent.parent / "apps.json"


@dataclass
class App:
    id: str
    label: str
    cmd: list[str] | None            # how to start it; None = can be closed but not started from here
    aliases: list[str] = field(default_factory=list)
    process: str | None = None       # image name used to close it, e.g. "chrome.exe"; None = can't be closed

    def names(self) -> list[str]:
        return [self.id, self.label, *self.aliases]


def _find(*candidates: str) -> str | None:
    """First candidate that exists as a path, or is found on PATH."""
    for c in candidates:
        expanded = os.path.expandvars(c)
        if Path(expanded).is_file():
            return expanded
        found = shutil.which(expanded)
        if found:
            return found
    return None


def _defaults() -> list[App]:
    if os.name != "nt":
        return []
    pf, pf86, local = (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
    chrome = _find(rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Google\Chrome\Application\chrome.exe",
                   rf"{local}\Google\Chrome\Application\chrome.exe")
    edge = _find(rf"{pf86}\Microsoft\Edge\Application\msedge.exe", rf"{pf}\Microsoft\Edge\Application\msedge.exe")
    apps = [
        App("notepad", "Notepad", [shutil.which("notepad.exe") or "notepad.exe"], ["text editor"], "notepad.exe"),
        App("calculator", "Calculator", ["calc.exe"], ["calc"], "CalculatorApp.exe"),
        App("explorer", "File Explorer", ["explorer.exe"], ["file manager", "explorer"], None),  # never closed
        App("vscode", "VS Code", None, ["vs code", "visual studio code"], "Code.exe"),  # opened by its own command
    ]
    wt = shutil.which("wt.exe")
    if wt:
        apps.append(App("terminal", "Terminal", [wt], ["windows terminal", "command line"], "WindowsTerminal.exe"))
    if chrome:
        apps.append(App("chrome", "Chrome", [chrome], ["google chrome", "browser"], "chrome.exe"))
    if edge:
        apps.append(App("edge", "Edge", [edge], ["microsoft edge"], "msedge.exe"))
    return apps


def load_apps() -> dict[str, App]:
    apps = {a.id: a for a in _defaults()}
    try:
        extra = json.loads(APPS_FILE.read_text(encoding="utf-8")).get("apps", {})
    except (OSError, ValueError):
        extra = {}
    for app_id, spec in extra.items():
        if not isinstance(spec, dict):
            continue
        cmd = spec.get("cmd")
        cmd = [cmd] if isinstance(cmd, str) else cmd
        if cmd and not _find(cmd[0]):
            continue  # listed but not installed here
        apps[app_id] = App(app_id, spec.get("label", app_id), cmd, list(spec.get("aliases", [])), spec.get("process"))
    return apps


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_app(text: str, apps: dict[str, App]) -> App | None:
    """The allowed app named in `text` (exact, ignoring case, spaces and punctuation), else None.
    Longest name wins, so "google chrome" beats "chrome"."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    grams = {"".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
    best, best_len = None, 0
    for app in apps.values():
        for name in app.names():
            n = _norm(name)
            if len(n) >= 3 and n in grams and len(n) > best_len:
                best, best_len = app, len(n)
    return best


# ---- anything with a Start Menu shortcut: open by name only -------------------------------------

SKIP_SHORTCUT = re.compile(r"uninstall|readme|release notes|documentation|license|website|help\b|support|manual", re.I)
FILLER = {"the", "my", "a", "an", "please", "app", "application", "program", "software", "can", "you", "for",
          "me", "up", "pull", "bring", "fire", "start", "open", "launch"}
_start_menu_cache: tuple[float, dict[str, str]] = (0.0, {})


def start_menu_apps() -> dict[str, str]:
    """Shortcut name -> path of its .lnk, from the all-users and per-user Start Menu (cached 10 minutes)."""
    global _start_menu_cache
    if time.monotonic() - _start_menu_cache[0] < 600 and _start_menu_cache[1]:
        return _start_menu_cache[1]
    found: dict[str, str] = {}
    if os.name == "nt":
        for env in ("ProgramData", "APPDATA"):
            base = Path(os.environ.get(env, "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if not base.is_dir():
                continue
            for i, lnk in enumerate(base.rglob("*.lnk")):
                if i > 3000:
                    break
                if not SKIP_SHORTCUT.search(lnk.stem):
                    found.setdefault(lnk.stem, str(lnk))
    _start_menu_cache = (time.monotonic(), found)
    return found


def find_installed(text: str) -> tuple[str, str] | None:
    """(shortcut name, path) for what the user asked to open, or None.
    Everything after "open" (minus filler words like "the" or "app") must be the start of one shortcut's
    name, so "open MySQL Workbench" finds "MySQL Workbench 8.0 CE" while "open the python file" or
    "open the login page" find nothing and stay prompts for the agent."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in FILLER]
    wanted = "".join(words)
    if len(wanted) < 4 or not words:
        return None
    hits = [(name, path) for name, path in start_menu_apps().items() if _norm(name).startswith(wanted)]
    return min(hits, key=lambda h: len(h[0])) if hits else None
