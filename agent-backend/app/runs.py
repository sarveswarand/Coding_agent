"""Agent runs that live on the server, not on the client's connection.

A phone loses signal and goes to sleep. Each run is a background task that appends its events to
a per-session log; a client attaches with a cursor (`after`), reads what it missed, then follows
live. Dropping the connection never stops the run: only an explicit cancel does.
"""
import asyncio
import os

from .cli_runner import run_claude
from .orchestrator import run_agent

MAX_LOG = 5000  # events kept per session; the oldest are dropped past this
KEEPALIVE_S = 15


def speech_for(ev: dict) -> str | None:
    """A short sentence the phone can read aloud for this event, built from what really happened."""
    t = ev["type"]
    if t == "tool_call":
        a, n = ev.get("input") or {}, ev["name"]
        return {
            "read_file": f"Reading {os.path.basename(str(a.get('path', 'a file')))}.",
            "list_dir": f"Listing {a.get('path') or 'the folder'}.",
            "find_files": f"Looking for files matching {a.get('pattern', '')}.",
            "search": f"Searching for {a.get('pattern', '')}.",
            "git_status": "Checking git status.",
            "git_diff": "Checking the changes.",
            "edit_file": f"Editing {os.path.basename(str(a.get('path', 'a file')))}.",
            "run_command": f"Running {str(a.get('command', 'a command'))[:80]}.",
        }.get(n, f"Using {n}.")
    if t == "tool_result" and ev.get("is_error"):
        return "That step failed."
    if t == "approval_request":
        if ev["name"] == "run_command":
            return f"I want to run {str(ev.get('preview', ''))[:80]}. Approve?"
        if ev["name"] == "edit_file":
            return "I want to make an edit. Approve?"
        return f"I want to use {ev['name']}. Approve?"
    if t == "auto_approved":
        return None
    if t == "stopped":
        return "Stopped."
    if t == "error":
        return "Something went wrong."
    return None


class Session:
    def __init__(self):
        self.log: list[dict] = []
        self.base = 0  # seq of log[0]
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.workspace: str | None = None  # the folder the latest run worked in

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def next_seq(self) -> int:
        return self.base + len(self.log)

    def emit(self, ev: dict) -> None:
        ev = dict(ev, seq=self.next_seq)
        if "speech" not in ev and (s := speech_for(ev)):
            ev["speech"] = s
        self.log.append(ev)
        if len(self.log) > MAX_LOG:
            drop = len(self.log) - MAX_LOG
            del self.log[:drop]
            self.base += drop
        old, self.changed = self.changed, asyncio.Event()
        old.set()  # wake everyone waiting for news

    async def follow(self, after: int):
        """Yield events with seq >= after; None every KEEPALIVE_S while idle; return once the run is over."""
        i = max(after, self.base)
        while True:
            news = self.changed
            while i < self.next_seq:
                yield self.log[i - self.base]
                i += 1
            if not self.active:
                return
            try:
                await asyncio.wait_for(news.wait(), KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield None


sessions: dict[str, Session] = {}


def get(session_id: str) -> Session:
    return sessions.setdefault(session_id, Session())


async def _drive(s: Session, mode: str = "builtin", **kw) -> None:
    runner = run_claude if mode == "claude" else run_agent
    try:
        async for ev in runner(inbox=s.inbox, narrate=True, **kw):
            s.emit(ev)
    except asyncio.CancelledError:
        s.emit({"type": "stopped"})  # not re-raised: the task ends normally, history is repaired by run_agent
    except Exception as e:
        s.emit({"type": "error", "message": str(e)})
    finally:
        while not s.inbox.empty():  # messages that arrived too late must not leak into the next run
            s.inbox.get_nowait()


def start(session_id: str, **kw) -> Session:
    s = get(session_id)
    s.workspace = kw["workspace"]
    s.emit({"type": "user", "text": kw["user_text"]})
    s.task = asyncio.create_task(_drive(s, session_id=session_id, **kw))
    return s


def steer(session_id: str, text: str) -> bool:
    s = sessions.get(session_id)
    if not (s and s.active):
        return False
    s.inbox.put_nowait(text)
    return True


def cancel(session_id: str) -> bool:
    s = sessions.get(session_id)
    if s and s.active:
        s.task.cancel()
        return True
    return False
