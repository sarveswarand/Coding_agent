"""Token usage tracking: per request (log + usage.jsonl) and per session (totals).

Costs are estimates from list prices and only exist for models in PRICES;
other models are still counted in tokens, just without a dollar figure.
"""
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn terminal
LOG_FILE = Path(__file__).resolve().parent.parent / "usage.jsonl"

# $ per 1M tokens: (input, output, cache_read). Cache writes are billed at 1.25x input.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

sessions: dict[str, dict] = {}  # session_id -> running totals


def cost_of(model: str, u: dict) -> float | None:
    prices = PRICES.get(model)
    if not prices:
        return None
    p_in, p_out, p_cache = prices
    return (u["input"] * p_in + u["cache_write"] * p_in * 1.25
            + u["cache_read"] * p_cache + u["output"] * p_out) / 1_000_000


def record(session_id: str, model: str, u: dict) -> dict:
    """u = {"input","output","cache_read","cache_write"} (input excludes cached tokens)."""
    cost = cost_of(model, u)
    t = sessions.setdefault(session_id, {"calls": 0, "input": 0, "output": 0, "cache_read": 0,
                                         "cache_write": 0, "cost": 0.0, "unpriced_calls": 0})
    t["calls"] += 1
    for k in ("input", "output", "cache_read", "cache_write"):
        t[k] += u[k]
    if cost is None:
        t["unpriced_calls"] += 1
    else:
        t["cost"] += cost

    log.info("usage %s | in %d | out %d | cache_read %d | cache_write %d | %s | session total: in %d out %d",
             model, u["input"], u["output"], u["cache_read"], u["cache_write"],
             "$%.4f" % cost if cost is not None else "cost n/a",
             t["input"] + t["cache_read"] + t["cache_write"], t["output"])
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session_id,
                                "model": model, **u, "cost": cost}) + "\n")
    except OSError as e:  # never let logging break a chat
        log.warning("could not write %s: %s", LOG_FILE, e)
    return t
