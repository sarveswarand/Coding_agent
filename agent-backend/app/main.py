import asyncio
import json
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import usage  # noqa: E402
from .gateway import list_models  # noqa: E402
from .orchestrator import pending, run_agent  # noqa: E402

app = FastAPI()


class Problem(BaseModel):
    line: int
    severity: str
    message: str


class EditorContext(BaseModel):
    """What the user has open in VS Code when they send a message."""
    file: str
    cursor_line: int
    unsaved: bool = False
    selection: str | None = None
    selection_start: int | None = None
    selection_end: int | None = None
    problems: list[Problem] = []


class ChatReq(BaseModel):
    session_id: str
    workspace: str
    message: str
    model: str | None = None
    context: EditorContext | None = None
    approval_mode: Literal["ask", "autoEdits"] = "ask"
    allowed_commands: list[str] = []


class ApproveReq(BaseModel):
    id: str
    approved: bool
    always: bool = False  # "Always allow": run this exact command without asking for the rest of the chat


class CancelReq(BaseModel):
    session_id: str


stops: dict[str, asyncio.Event] = {}  # session_id -> set when the user presses Stop


@app.get("/models")
async def models():
    return list_models()


@app.get("/usage")
async def usage_totals():
    """Running token totals for every chat session since the backend started."""
    return usage.sessions


@app.post("/chat")
async def chat(req: ChatReq):
    async def stream():
        # While the agent waits on a human approval nothing is sent, and the
        # extension's fetch drops idle connections after ~5 min. Send an SSE
        # comment every 15s so the stream stays alive.
        events = run_agent(req.session_id, req.workspace, req.message, req.model,
                           context=req.context.model_dump() if req.context else None,
                           approval_mode=req.approval_mode,
                           allowed_commands=req.allowed_commands).__aiter__()
        stop = stops[req.session_id] = asyncio.Event()
        stop_wait = asyncio.ensure_future(stop.wait())
        nxt = asyncio.ensure_future(events.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait({nxt, stop_wait}, timeout=15,
                                             return_when=asyncio.FIRST_COMPLETED)
                if stop_wait in done:
                    # Stop pressed: cancel whatever the agent is doing (model call,
                    # waiting for approval, ...). run_agent's finally repairs history.
                    nxt.cancel()
                    await asyncio.gather(nxt, return_exceptions=True)
                    yield f"data: {json.dumps({'type': 'stopped'})}\n\n"
                    break
                if not done:
                    yield ": ping\n\n"
                    continue
                try:
                    ev = nxt.result()
                except StopAsyncIteration:
                    break
                yield f"data: {json.dumps(ev)}\n\n"
                nxt = asyncio.ensure_future(events.__anext__())
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            nxt.cancel()
            stop_wait.cancel()
            if stops.get(req.session_id) is stop:
                del stops[req.session_id]
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/cancel")
async def cancel(req: CancelReq):
    stop = stops.get(req.session_id)
    if stop:
        stop.set()
    return {"ok": True, "running": stop is not None}


@app.post("/approve")
async def approve(req: ApproveReq):
    fut = pending.pop(req.id, None)
    if fut and not fut.done():
        fut.set_result((req.approved, req.always))
    return {"ok": True}
