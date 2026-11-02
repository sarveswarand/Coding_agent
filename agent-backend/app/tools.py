import difflib
import re
import shutil
import subprocess
from pathlib import Path

READ_ONLY = {"read_file", "list_dir", "search"}
SKIP_DIRS = ("node_modules", "venv", ".git", "__pycache__")

TOOL_SCHEMAS = [
    {"name": "read_file", "description": "Read a text file in the workspace.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_dir", "description": "List files in a directory ('.' for the workspace root).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "search", "description": "Search file contents for a regex.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "edit_file", "description": "Replace exact text `old` with `new` in a file. Use old='' to create a new file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                      "required": ["path", "old", "new"]}},
    {"name": "run_command", "description": "Run a shell command in the workspace root.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]


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


def _search(root: str, pattern: str) -> str:
    if shutil.which("rg"):
        r = subprocess.run(["rg", "-n", "--max-count", "5", "-g", "!node_modules", "-g", "!venv",
                            pattern, "."], cwd=root, capture_output=True, text=True, timeout=30)
        return r.stdout[:20_000] or "No matches"
    rx = re.compile(pattern)
    out = []
    for f in Path(root).rglob("*"):
        if f.is_file() and not any(s in f.parts for s in SKIP_DIRS):
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{f.relative_to(root)}:{i}:{line}")
            except Exception:
                pass
    return "\n".join(out[:200]) or "No matches"


def run_tool(root: str, name: str, a: dict) -> str:
    if name == "read_file":
        return _safe(root, a["path"]).read_text(encoding="utf-8")[:50_000]
    if name == "list_dir":
        return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in _safe(root, a["path"]).iterdir()))
    if name == "search":
        return _search(root, a["pattern"])
    if name == "edit_file":
        p, new_text, _ = _edit_result(root, a)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_text, encoding="utf-8")
        return f"Edited {a['path']}"
    if name == "run_command":
        r = subprocess.run(a["command"], shell=True, cwd=root, capture_output=True, text=True, timeout=120)
        return (f"exit={r.returncode}\n{r.stdout}\n{r.stderr}")[-20_000:]
    raise ValueError(f"Unknown tool {name}")
