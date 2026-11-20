import asyncio
import json
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import assistant, cli_runner, local_llm, runs, usage  # noqa: E402
from .gateway import list_models  # noqa: E402
from .orchestrator import pending, run_agent  # noqa: E402
from .security import allowed_roots, check_workspace, is_direct_local, require_local_or_token, require_token  # noqa: E402

app = FastAPI()
# Endpoints used by the VS Code extension: a direct local connection, or a valid token.
local = APIRouter(dependencies=[Depends(require_local_or_token)])
# Endpoints for remote clients such as the phone app: always need the token.
remote = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


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


@app.on_event("startup")
async def _warm_local_model():
    asyncio.create_task(local_llm.warm_up())  # background: don't hold up startup


@local.get("/models")
async def models():
    return list_models()


@local.get("/usage")
async def usage_totals():
    """Running token totals for every chat session since the backend started."""
    return usage.sessions


@local.post("/chat")
async def chat(req: ChatReq, request: Request):
    req.workspace = check_workspace(req.workspace, remote=not is_direct_local(request))

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


@local.post("/cancel")
async def cancel(req: CancelReq):
    stop = stops.get(req.session_id)
    if stop:
        stop.set()
    return {"ok": True, "running": stop is not None}


@local.post("/approve")
async def approve(req: ApproveReq):
    fut = pending.pop(req.id, None)
    if fut and not fut.done():
        fut.set_result((req.approved, req.always))
    return {"ok": True}


# ---- Remote API (phone app) --------------------------------------------------

class RunReq(BaseModel):
    session_id: str
    workspace: str
    message: str
    mode: Literal["builtin", "claude"] = "builtin"  # which agent does the work
    model: str | None = None
    approval_mode: Literal["ask", "autoEdits"] = "ask"
    allowed_commands: list[str] = []


class SteerReq(BaseModel):
    message: str


@remote.get("/models")
async def v1_models():
    """Local models only: the phone never uses a cloud API key."""
    return [m for m in list_models() if m["provider"] == "ollama"]


@remote.get("/workspaces")
async def v1_workspaces():
    """The allowed roots and the project folders directly inside them."""
    skip = {"node_modules", "venv", "__pycache__"}
    folders = []
    for r in allowed_roots():
        if r.is_dir():
            folders.append({"name": r.name or str(r), "path": str(r)})
            folders += [{"name": f"{r.name}/{d.name}", "path": str(d)} for d in sorted(r.iterdir())
                        if d.is_dir() and not d.name.startswith(".") and d.name not in skip][:200]
    return {"folders": folders}


@remote.post("/runs")
async def v1_start(req: RunReq):
    """Start a run, or, if one is already going in this session, steer it with the new message."""
    workspace = check_workspace(req.workspace, remote=True)
    if runs.steer(req.session_id, req.message):
        return {"steered": True, "next_seq": runs.get(req.session_id).next_seq}
    model = None
    if req.mode == "claude":
        if not cli_runner.available():
            raise HTTPException(503, "Claude Code isn't installed on this PC (set CLAUDE_BIN to its path).")
    else:  # the built-in agent: a local model only, whatever the client asked for
        model = local_llm.local_model_id()
        if not model:
            raise HTTPException(503, "No local model found. Start Ollama and pull one (e.g. `ollama pull qwen3:8b`).")
    s = runs.start(req.session_id, mode=req.mode, workspace=workspace, user_text=req.message, model=model,
                   approval_mode=req.approval_mode, allowed_commands=req.allowed_commands)
    return {"steered": False, "next_seq": s.next_seq}


@remote.post("/say")
async def v1_say(req: RunReq):
    """What the user said by voice. Commands for the assistant itself (open Claude Code, what's
    happening?) are handled here; anything else goes to the coding agent as a prompt."""
    workspace = check_workspace(req.workspace, remote=True)
    s = runs.get(req.session_id)
    intent = assistant.classify(req.message)
    if intent == "prompt":
        return {**await v1_start(req), "handled": None, "active": True}
    s.emit({"type": "said", "text": req.message})
    if intent == "open":
        reply = await assistant.open_claude_code(workspace, cli_runner.session_for(req.session_id))
    else:
        reply = await assistant.status(s)
    s.emit({"type": "voice", "text": reply, "speech": reply})
    return {"handled": intent, "active": s.active, "next_seq": s.next_seq}


@remote.get("/sessions/{session_id}")
async def v1_session(session_id: str):
    s = runs.get(session_id)
    return {"active": s.active, "first_seq": s.base, "next_seq": s.next_seq}


@remote.get("/sessions/{session_id}/events")
async def v1_events(session_id: str, after: int = 0):
    """Server-sent events from sequence number `after` on; replays what was missed, then follows live."""
    s = runs.get(session_id)

    async def stream():
        async for ev in s.follow(after):
            yield ": ping\n\n" if ev is None else f"data: {json.dumps(ev)}\n\n"
        yield f"data: {json.dumps({'type': 'idle', 'seq': s.next_seq})}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@remote.post("/sessions/{session_id}/steer")
async def v1_steer(session_id: str, req: SteerReq):
    return {"ok": runs.steer(session_id, req.message)}


@remote.post("/sessions/{session_id}/cancel")
async def v1_cancel(session_id: str):
    return {"ok": True, "running": runs.cancel(session_id)}


@remote.post("/approve")
async def v1_approve(req: ApproveReq):
    return await approve(req)


app.include_router(local)
app.include_router(remote)


@app.get("/")
async def root():
    return RedirectResponse("/app/")


# The phone app: plain files, no secrets in them (the API behind it needs the token).
app.mount("/app", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="app")
