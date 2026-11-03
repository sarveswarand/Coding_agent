import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

READ_ONLY = {"read_file", "list_dir", "find_files", "search", "git_status", "git_diff"}
SKIP_DIRS = {"node_modules", "venv", ".venv", ".git", "__pycache__", "dist", "build", "out",
             ".next", "coverage", ".idea", ".gradle"}
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".pdf", ".zip", ".gz",
               ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".lock", ".map", ".exe", ".dll"}
MAX_SEARCH_FILE_BYTES = 1_000_000
SEARCH_BUDGET_S = 20
READ_MAX_LINES = 2000
READ_MAX_CHARS = 50_000
# ripgrep options use the --opt=value form so a model-supplied value can never
# be parsed as an option of its own (e.g. a pattern of "--pre=cmd").
RG_SKIP = [f"--glob=!{d}" for d in sorted(SKIP_DIRS)]
# Read-only git: no pager, and no repo-configured programs (fsmonitor; diff drivers are off in _git_diff).
GIT = ["git", "--no-pager", "-c", "core.fsmonitor=false"]

TOOL_SCHEMAS = [
    {"name": "read_file",
     "description": "Read a text file. Paths are relative to the workspace root. Each line starts with its "
                    "number and a tab; leave that prefix out of edit_file's `old`. Shows up to 2000 lines; "
                    "use start_line/end_line (1-based, inclusive) to read other parts of long files.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                                     "end_line": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "list_dir", "description": "List files in a directory ('.' for the workspace root).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "find_files",
     "description": "Find files by name. `pattern` is a glob: '*.py' or 'test_*' match file names at any depth, "
                    "'src/**/*.ts' matches paths. Skips node_modules, build output and git-ignored files.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"},
                                     "path": {"type": "string", "description": "Folder to look in (default: workspace root)"}},
                      "required": ["pattern"]}},
    {"name": "search",
     "description": "Search file contents for a regex, case-insensitive unless case_sensitive is set. "
                    "Returns path:line:text, at most 5 matches per file.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"},
                                     "literal": {"type": "boolean", "description": "Match the pattern as plain text"},
                                     "case_sensitive": {"type": "boolean"},
                                     "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
                                     "context": {"type": "integer", "description": "Lines of context around each match (0-5)"}},
                      "required": ["pattern"]}},
    {"name": "git_status", "description": "Show the current git branch and the changed and untracked files.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "git_diff",
     "description": "Show uncommitted changes to tracked files (git diff), optionally for one path or only staged changes.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "staged": {"type": "boolean"}}}},
    {"name": "edit_file", "description": "Replace exact text `old` with `new` in a file. Use old='' to create a new file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                      "required": ["path", "old", "new"]}},
    {"name": "run_command", "description": "Run a shell command in the workspace root.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]
TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}


def unknown_tool_message(name: str) -> str:
    """Error text for a made-up tool name; lists the real ones so the model can retry correctly."""
    return f"Unknown tool '{name}'. Available tools: {', '.join(sorted(TOOL_NAMES))}."


def _safe(root: str, rel: str) -> Path:
    r = Path(root).resolve()
    p = (r / rel).resolve()
    if not p.is_relative_to(r):
        raise ValueError("Path escapes workspace")
    return p


def _edit_result(root, a):
    p = _safe(root, a["path"])
    old_text = p.read_text(encoding="utf-8") if p.exists() else ""
    if a["old"] == "":
        new_text = a["new"]
    else:
        if old_text.count(a["old"]) != 1:
            raise ValueError("`old` must match exactly once in the file")
        new_text = old_text.replace(a["old"], a["new"])
    diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                        a["path"], a["path"]))
    return p, new_text, diff


def preview(root, name, a) -> str:
    """Text shown to the user in the approval card."""
    if name == "edit_file":
        return _edit_result(root, a)[2] or "(no changes)"
    if name == "run_command":
        return a["command"]
    return ""


def _read_file(root: str, a: dict) -> str:
    p = _safe(root, a["path"])
    if p.is_dir():
        raise ValueError(f"{a['path']} is a directory; use list_dir")
    with open(p, "rb") as fh:
        if b"\0" in fh.read(8192):
            return f"(binary file, {p.stat().st_size:,} bytes; not shown)"
    start = max(1, int(a.get("start_line") or 1))
    end = min(int(a.get("end_line") or start + READ_MAX_LINES - 1), start + READ_MAX_LINES - 1)
    out, chars, total = [], 0, 0
    with open(p, encoding="utf-8", errors="replace") as fh:
        for total, line in enumerate(fh, 1):
            if start <= total <= end and chars < READ_MAX_CHARS:
                line = line.rstrip("\n")
                if len(line) > 2000:
                    line = line[:2000] + " ...(line cut)"
                out.append(f"{total:>6}\t{line}")
                chars += len(line)
    if not out:
        return f"(nothing in lines {start}-{end}; the file has {total} lines)" if total else "(empty file)"
    last = start + len(out) - 1
    if start > 1 or last < total:
        out.append(f"(lines {start}-{last} of {total}; use start_line/end_line to see more)")
    return "\n".join(out)


def _rel(root: str, p: Path) -> str:
    return p.relative_to(Path(root).resolve()).as_posix()


def _find_files(root: str, a: dict) -> str:
    base, pattern = _safe(root, a.get("path") or "."), a["pattern"]
    if shutil.which("rg"):
        r = subprocess.run(["rg", "--files", f"--glob={pattern}", *RG_SKIP, "--path-separator=/", "--", "."],
                           cwd=base, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        if r.returncode == 2 and not r.stdout:
            raise ValueError(r.stderr.strip()[:500])
        files = [_rel(root, base / f.removeprefix("./")) for f in r.stdout.splitlines()]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                f = Path(dirpath) / name
                if fnmatch.fnmatch(f.relative_to(base).as_posix() if "/" in pattern else name, pattern):
                    files.append(_rel(root, f))
    if not files:
        return "No files found"
    files.sort()
    more = f"\n({len(files) - 200} more not shown)" if len(files) > 200 else ""
    return "\n".join(files[:200]) + more


def _search(root: str, a: dict) -> str:
    pattern, glob = a["pattern"], a.get("glob")
    literal, case = bool(a.get("literal")), bool(a.get("case_sensitive"))
    ctx = max(0, min(int(a.get("context") or 0), 5))
    if shutil.which("rg"):
        args = ["rg", "-n", "--max-count", "5", "--max-columns", "300", "--path-separator=/",
                "--case-sensitive" if case else "--ignore-case", *(["--fixed-strings"] if literal else []),
                f"--context={ctx}", *([f"--glob={glob}"] if glob else []), *RG_SKIP,
                f"--regexp={pattern}", "--", "."]
        r = subprocess.run(args, cwd=root, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        if r.returncode == 2 and not r.stdout:  # 2 = error, e.g. an invalid regex
            raise ValueError(r.stderr.strip()[:500])
        return r.stdout[:10_000] or "No matches"
    # Fallback when ripgrep isn't installed: prune skipped dirs before walking
    # into them (rglob would traverse node_modules first), skip binary/large
    # files, and stop after a time budget.
    rx = re.compile(re.escape(pattern) if literal else pattern, 0 if case else re.IGNORECASE)
    out, matches = [], 0
    deadline = time.monotonic() + SEARCH_BUDGET_S
    timed_out = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if time.monotonic() > deadline:
                timed_out = True
                break
            f = Path(dirpath) / name
            rel = f.relative_to(root).as_posix()
            if f.suffix.lower() in BINARY_EXTS or (glob and not fnmatch.fnmatch(rel if "/" in glob else name, glob)):
                continue
            try:
                if f.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                lines = f.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for i in [i for i, line in enumerate(lines) if rx.search(line)][:5]:
                for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                    sep = ":" if j == i else "-"  # same layout as rg: match lines use ':'
                    out.append(f"{rel}{sep}{j + 1}{sep}{lines[j][:300]}")
                if ctx:
                    out.append("--")
                matches += 1
                if matches >= 100:
                    return "\n".join(out) + "\n(truncated at 100 matches)"
        if timed_out:
            break
    result = "\n".join(out) or "No matches"
    return result + "\n(search stopped early: time limit reached)" if timed_out else result


def _git(root: str, *args: str) -> str:
    r = subprocess.run([*GIT, *args], cwd=root, capture_output=True, encoding="utf-8", errors="replace",
                       timeout=30, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip()[:500] or f"git exited with code {r.returncode}")
    return r.stdout


def git_branch(root: str) -> str:
    """Current branch, or '' when the folder isn't a git repo or git isn't installed."""
    try:
        return _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def _git_diff(root: str, a: dict) -> str:
    args = ["diff", "--no-ext-diff", "--no-textconv", *(["--cached"] if a.get("staged") else [])]
    if a.get("path"):
        args += ["--", str(_safe(root, a["path"]))]
    out = _git(root, *args)
    if len(out) > 20_000:
        out = out[:20_000] + "\n(diff truncated; ask for one path at a time)"
    return out or "(no changes)"


def run_tool(root: str, name: str, a: dict) -> str:
    if name == "read_file":
        return _read_file(root, a)
    if name == "list_dir":
        return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in _safe(root, a["path"]).iterdir()))
    if name == "find_files":
        return _find_files(root, a)
    if name == "search":
        return _search(root, a)
    if name == "git_status":
        return _git(root, "status", "--short", "--branch")
    if name == "git_diff":
        return _git_diff(root, a)
    if name == "edit_file":
        p, new_text, _ = _edit_result(root, a)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_text, encoding="utf-8")
        return f"Edited {a['path']}"
    if name == "run_command":
        r = subprocess.run(a["command"], shell=True, cwd=root, capture_output=True, text=True, timeout=120)
        return (f"exit={r.returncode}\n{r.stdout}\n{r.stderr}")[-20_000:]
    raise ValueError(unknown_tool_message(name))
