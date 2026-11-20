"""The voice layer's own brain: a small local model (Ollama). Nothing here ever calls a cloud API."""
import asyncio
import logging
import os

from .gateway import OLLAMA_HOST, OLLAMA_PREFIX, _ollama_installed

log = logging.getLogger("uvicorn.error")


def local_model_name() -> str | None:
    """LOCAL_MODEL from .env, else the first model Ollama has installed."""
    explicit = os.environ.get("LOCAL_MODEL", "").strip()
    if explicit:
        return explicit.removeprefix(OLLAMA_PREFIX)
    installed = _ollama_installed()
    return installed[0] if installed else None


def local_model_id() -> str | None:
    name = local_model_name()
    return OLLAMA_PREFIX + name if name else None


async def ask(system: str, user: str, max_tokens: int = 120, timeout: float = 15.0,
              schema: dict | None = None) -> str | None:
    """One short completion from the local model, or None if it isn't available or too slow
    (callers then fall back to a plain answer, so a cold or busy model never blocks the voice).
    With `schema` (a JSON schema) the reply is forced to be JSON of that shape."""
    name = await asyncio.to_thread(local_model_name)
    if not name:
        return None
    try:
        from ollama import AsyncClient
        res = await asyncio.wait_for(
            AsyncClient(host=OLLAMA_HOST).chat(
                model=name, think=False, keep_alive="30m", format=schema,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                options={"temperature": 0.2, "num_predict": max_tokens}),
            timeout)
        return (res["message"]["content"] or "").strip() or None
    except Exception as e:
        log.warning("local model call failed: %s", repr(e))
        return None


async def warm_up() -> None:
    """Load the model into memory at startup so the first spoken status isn't slow."""
    await ask("Reply with one word.", "hi", max_tokens=2, timeout=300)
