import asyncio
import uuid

from .gateway import default_model, get_provider
from .tools import READ_ONLY, TOOL_SCHEMAS, preview, run_tool

SYSTEM = ("You are OrgAI, a coding agent working inside the user's repository. "
          "Explore with tools before answering. Make minimal, correct edits. "
          "Never claim you ran something you didn't.")

pending: dict[str, asyncio.Future] = {}  # approval_id -> Future[bool]
sessions: dict[str, list] = {}           # session_id -> provider-neutral messages


async def run_agent(session_id, workspace, user_text, model=None):
    msgs = sessions.setdefault(session_id, [])
    msgs.append({"role": "user", "text": user_text})
    model = model or default_model()
    provider = get_provider(model)

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
                    if not await fut:
                        result.update(content="User rejected this action.", is_error=True)
                        results.append(result)
                        continue
                result["content"] = await asyncio.to_thread(run_tool, workspace, name, args)
            except Exception as e:
                result.update(content=f"Error: {e}", is_error=True)
            results.append(result)
            yield {"type": "tool_result", "name": name}
        msgs.append({"role": "tool", "results": results})
    yield {"type": "done"}
