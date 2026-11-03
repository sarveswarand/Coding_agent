"""Benchmark: how well does a model drive the agent loop on small coding tasks?

Each task runs in a throwaway project in a temp dir, through the real agent loop
(app.orchestrator.run_agent). Edits are auto-approved (the file tools are jailed
to the temp dir); commands are auto-approved only if they look like running
python/pytest. Success is checked on the resulting files, not on what the model says.

Usage (from agent-backend):  python -m bench.run_bench "ollama/qwen2.5-coder:7b" gemini-3.5-flash-lite
"""
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app import orchestrator as o  # noqa: E402
from app.gateway import get_provider  # noqa: E402

SAFE_CMD = re.compile(r"^(python3?|py|pytest)\b")
UNSAFE = ("&&", ";", "|", ">", "<", "rm ", "del ", "rmdir", "format", " -c ")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_tests(root: Path) -> bool:
    r = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=root, capture_output=True, text=True, timeout=60)
    return r.returncode == 0


# ---------- tasks ----------
def t1_setup(root):
    (root / "pricing.py").write_text("def apply_discount(price, percent):\n    return price * (1 - percent / 100)\n")
    (root / "orders.py").write_text("from pricing import apply_discount\n\n\ndef total(items):\n    return sum(apply_discount(p, 10) for p in items)\n")
    (root / "utils.py").write_text('def fmt(x):\n    return f"${x:.2f}"\n')
    return {}


def t1_check(root, final_text, ctx):
    t = final_text.lower()
    return "apply_discount" in t and "pricing.py" in t


def t2_setup(root):
    (root / "config.py").write_text("DEBUG = False\nMAX_RETRIES = 3\nTIMEOUT = 30\n")
    return {}


def t2_check(root, final_text, ctx):
    return (root / "config.py").read_text().replace("\r\n", "\n") == "DEBUG = False\nMAX_RETRIES = 5\nTIMEOUT = 30\n"


def t3_setup(root):
    (root / "stats.py").write_text("def average(nums):\n    if not nums:\n        return 0\n    return sum(nums) / (len(nums) + 1)\n\n\n"
                                   "def percentage(part, whole):\n    return part / whole * 100\n")
    (root / "test_stats.py").write_text(
        "import unittest\nfrom stats import average, percentage\n\n\nclass T(unittest.TestCase):\n"
        "    def test_avg(self):\n        self.assertEqual(average([2, 4, 6]), 4)\n"
        "    def test_empty(self):\n        self.assertEqual(average([]), 0)\n"
        "    def test_single(self):\n        self.assertEqual(average([5]), 5)\n"
        "    def test_pct(self):\n        self.assertEqual(percentage(1, 4), 25)\n")
    return {"test_sha": sha(root / "test_stats.py")}


def t3_check(root, final_text, ctx):
    return sha(root / "test_stats.py") == ctx["test_sha"] and run_tests(root)


def t4_setup(root):
    (root / "config.py").write_text('TRUSTED_PROXIES = {"10.0.0.1"}\n')
    (root / "client_ip.py").write_text(
        'def get_client_ip(headers, peer_ip):\n    fwd = headers.get("X-Forwarded-For")\n    if fwd:\n'
        '        return fwd.split(",")[0].strip()\n    return peer_ip\n')
    (root / "limiter.py").write_text(
        "from collections import defaultdict\n\nfrom client_ip import get_client_ip\n\nhits = defaultdict(int)\n\n\n"
        "def allow(headers, peer_ip, limit=5):\n    ip = get_client_ip(headers, peer_ip)\n    hits[ip] += 1\n    return hits[ip] <= limit\n")
    (root / "test_limiter.py").write_text(
        "import unittest\n\nimport limiter\n\n\nclass T(unittest.TestCase):\n    def setUp(self):\n        limiter.hits.clear()\n\n"
        "    def test_blocks_after_limit(self):\n        r = [limiter.allow({}, '1.2.3.4') for _ in range(6)]\n        self.assertEqual(r, [True] * 5 + [False])\n\n"
        "    def test_spoofed_header_does_not_reset_limit(self):\n"
        "        r = [limiter.allow({'X-Forwarded-For': f'9.9.9.{i}'}, '203.0.113.9') for i in range(6)]\n"
        "        self.assertEqual(r, [True] * 5 + [False])\n\n"
        "    def test_trusted_proxy_header_is_used(self):\n"
        "        a = [limiter.allow({'X-Forwarded-For': '7.7.7.7'}, '10.0.0.1') for _ in range(5)]\n"
        "        b = limiter.allow({'X-Forwarded-For': '8.8.8.8'}, '10.0.0.1')\n"
        "        self.assertTrue(all(a) and b)\n\n\nif __name__ == '__main__':\n    unittest.main()\n")
    return {"test_sha": sha(root / "test_limiter.py")}


def t4_check(root, final_text, ctx):
    return sha(root / "test_limiter.py") == ctx["test_sha"] and run_tests(root)


TASKS = [
    ("T1 explore + answer", t1_setup, t1_check,
     "Which function in this project computes the discount, and in which file is it defined? Answer in one sentence."),
    ("T2 one-line edit", t2_setup, t2_check, "In config.py, change MAX_RETRIES to 5. Change nothing else."),
    ("T3 fix a bug", t3_setup, t3_check,
     "The tests in test_stats.py are failing. Find the bug in stats.py and fix it (do not edit the tests), "
     "then run `python -m unittest` to confirm they pass."),
    ("T4 security fix", t4_setup, t4_check,
     "Users can bypass the login rate limiter by sending a spoofed X-Forwarded-For header. Fix it so the header is "
     "only trusted when the direct peer IP is in TRUSTED_PROXIES (config.py); otherwise the peer IP must be used. "
     "Do not edit the tests. Run `python -m unittest` to confirm all tests pass."),
]


# ---------- runner ----------
async def run_task(model, name, setup, check, prompt, timeout):
    root = Path(tempfile.mkdtemp(prefix="bench_"))
    ctx = setup(root)
    provider = get_provider(model)

    gen_tok = gen_sec = 0.0
    inline = 0
    orig = provider.generate

    async def wrapped(*a, **k):
        nonlocal gen_tok, gen_sec, inline
        turn = await orig(*a, **k)
        s = getattr(provider, "last_stats", None)
        if s:
            gen_tok += s["gen_tokens"]; gen_sec += s["gen_seconds"]; inline += int(s["inline_tool_call"])
        return turn

    provider.generate = wrapped
    m = {"calls": 0, "tool_calls": 0, "tool_errors": 0, "rejected_cmds": 0, "in": 0, "out": 0}
    texts = []
    status = "ok"
    t0 = time.time()

    transcript = []

    async def consume():
        async for ev in o.run_agent(f"bench-{name}-{model}-{time.time()}", str(root), prompt, model):
            if ev["type"] == "text":
                texts.append(ev["text"])
                transcript.append({"text": ev["text"][:600]})
            elif ev["type"] == "tool_call":
                m["tool_calls"] += 1
                transcript.append({"tool": ev["name"], "input": json.dumps(ev["input"])[:400]})
            elif ev["type"] == "tool_result" and ev.get("is_error"):
                m["tool_errors"] += 1
            elif ev["type"] == "usage":
                s = ev["session"]
                m["calls"], m["in"], m["out"] = s["calls"], s["input"] + s["cache_read"], s["output"]
            elif ev["type"] == "approval_request":
                ok = True
                if ev["name"] == "run_command":
                    cmd = ev["preview"].strip()
                    ok = bool(SAFE_CMD.match(cmd)) and not any(x in cmd for x in UNSAFE)
                    m["rejected_cmds"] += 0 if ok else 1
                o.pending[ev["id"]].set_result((ok, False))  # (approved, always allow)

    try:
        await asyncio.wait_for(consume(), timeout)
    except asyncio.TimeoutError:
        status = "timeout"
    except Exception as e:  # provider error etc.
        status = f"error: {str(e)[:120]}"
    finally:
        provider.generate = orig
    wall = time.time() - t0
    passed = False
    try:
        passed = bool(check(root, "\n".join(texts), ctx))
    except Exception as e:
        status += f" (check failed: {e})"
    return {"model": model, "task": name, "passed": passed, "status": status, "seconds": round(wall, 1),
            **m, "inline_tool_calls": inline, "transcript": transcript,
            "gen_tok_per_s": round(gen_tok / gen_sec, 1) if gen_sec else None}


async def main(models):
    results = []
    for model in models:
        timeout = 900 if model.startswith("ollama/") else 300
        for name, setup, check, prompt in TASKS:
            print(f"[{time.strftime('%H:%M:%S')}] {model} | {name} ...", flush=True)
            r = await run_task(model, name, setup, check, prompt, timeout)
            results.append(r)
            print("   ->", json.dumps({k: v for k, v in r.items() if k != "transcript"}), flush=True)
            for step in r["transcript"]:
                print("      ", json.dumps(step), flush=True)
    out = Path(__file__).resolve().parent / "results"
    out.mkdir(exist_ok=True)
    f = out / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps(results, indent=2))
    print("\nsaved", f)
    print("\n| model | task | pass | secs | calls | tools | tool errs | inline tool calls | in tok | out tok | tok/s |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['model']} | {r['task']} | {'PASS' if r['passed'] else 'FAIL'} ({r['status']}) | {r['seconds']} | {r['calls']} | "
              f"{r['tool_calls']} | {r['tool_errors']} | {r['inline_tool_calls']} | {r['in']} | {r['out']} | {r['gen_tok_per_s']} |")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["ollama/qwen2.5-coder:7b"]))
