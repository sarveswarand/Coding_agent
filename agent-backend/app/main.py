import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .gateway import list_models  # noqa: E402
from .orchestrator import pending, run_agent  # noqa: E402

app = FastAPI()


class ChatReq(BaseModel):
    session_id: str
    workspace: str
    message: str
    model: str | None = None


class ApproveReq(BaseModel):
    id: str
    approved: bool


@app.get("/models")
async def models():
    return list_models()


@app.post("/chat")
async def chat(req: ChatReq):
    async def stream():
        # While the agent waits on a human approval nothing is sent, and the
        # extension's fetch drops idle connections after ~5 min. Send an SSE
        # comment every 15s so the stream stays alive.
        events = run_agent(req.session_id, req.workspace, req.message, req.model).__aiter__()
        nxt = asyncio.ensure_future(events.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait({nxt}, timeout=15)
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
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/approve")
async def approve(req: ApproveReq):
    fut = pending.pop(req.id, None)
    if fut and not fut.done():
        fut.set_result(req.approved)
    return {"ok": True}
