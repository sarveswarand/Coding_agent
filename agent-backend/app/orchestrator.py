import asyncio
import logging
import uuid

from .gateway import default_model, get_provider
from .tools import READ_ONLY, TOOL_SCHEMAS, preview, run_tool

log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn terminal

SYSTEM = ("You are OrgAI, a coding agent working inside the user's repository. "
          "Explore with tools before answering. Make minimal, correct edits. "
          "Never claim you ran something you didn't.")

pending: dict[str, asyncio.Future] = {}  # approval_id -> Future[bool]
sessions: dict[str, list] = {}           # session_id -> provider-neutral messages


def _repair_history(msgs: list) -> None:
    """After a cancelled turn, leave the history valid for the next request:
    every tool call needs a result, and roles must make sense."""
    last = msgs[-1] if msgs else None
    if not last:
        return
    if last["role"] == "assistant" and last["tool_calls"]:
        msgs.append({"role": "tool", "results": [
            {"id": c["id"], "name": c["name"], "content": "Cancelled by user.", "is_error": True}
            for c in last["tool_calls"]]})
    elif last["role"] == "user":
        msgs.append({"role": "assistant", "text": "(Request interrupted)", "tool_calls": [], "raw": None})


async def run_agent(session_id, workspace, user_text, model=None):
    msgs = sessions.setdefault(session_id, [])
    msgs.append({"role": "user", "text": user_text})
    model = model or default_model()
    provider = get_provider(model)
    aid = None
    try:
        for _ in range(25):  # hard cap on loop iterations
            turn = await provider.generate(model, SYSTEM, msgs, TOOL_SCHEMAS)
            msgs.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw})
            if turn.text:
                yield {"type": "text", "text": turn.text}
            if not turn.tool_calls:
                break

            results = []
            for call in turn.tool_calls:
                name, args = call["name"], call["input"]
                yield {"type": "tool_call", "name": name, "input": args}
                result = {"id": call["id"], "name": name, "content": "", "is_error": False}
                try:
                    if name not in READ_ONLY:
                        aid = str(uuid.uuid4())
                        fut = asyncio.get_running_loop().create_future()
                        pending[aid] = fut
                        yield {"type": "approval_request", "id": aid, "name": name,
                               "preview": preview(workspace, name, args)}
                        log.info("waiting for approval of %s (%s)", name, aid)
                        approved = await fut
                        log.info("approval %s -> %s", aid, "approved" if approved else "rejected")
                        pending.pop(aid, None)
                        aid = None
                        if not approved:
                            result.update(content="User rejected this action.", is_error=True)
                            results.append(result)
                            continue
                    result["content"] = await asyncio.to_thread(run_tool, workspace, name, args)
                except Exception as e:
                    result.update(content=f"Error: {e}", is_error=True)
                results.append(result)
                if result["is_error"]:
                    log.warning("tool %s failed: %s", name, result["content"][:300])
                yield {"type": "tool_result", "name": name, "is_error": result["is_error"]}
            msgs.append({"role": "tool", "results": results})
        yield {"type": "done"}
    finally:
        # Runs on normal exit, on error, and when the turn is cancelled (Stop).
        if aid:
            pending.pop(aid, None)
        _repair_history(msgs)
