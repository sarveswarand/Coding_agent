"""Model Gateway: one interface over Anthropic and Gemini.

History is stored in a provider-neutral format so the model can be switched
between turns:
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_calls": [{"id","name","input"}], "raw": (provider, obj)|None}
  {"role": "tool", "results": [{"id","name","content","is_error"}]}
`raw` keeps the provider's own message (thinking blocks, thought signatures)
and is replayed verbatim when the same provider handles the next turn.
"""
import logging
import os
import re
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("uvicorn.error")

CUT_OFF = "(Reply cut off: it hit the output length limit. Ask me to continue, or to work in smaller steps.)"


class RateLimited(Exception):
    """A per-minute rate limit was hit; the same request can be retried after `retry_after` seconds."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Turn:
    text: str = ""
    tool_calls: list = field(default_factory=list)  # [{"id","name","input"}]
    raw: tuple | None = None                         # (provider_name, provider_obj)
    # input excludes cached tokens; output includes thinking tokens
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})


class AnthropicProvider:
    name = "anthropic"

    def __init__(self):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _convert(self, messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                if m.get("raw") and m["raw"][0] == self.name:
                    out.append({"role": "assistant", "content": m["raw"][1]})
                    continue
                blocks = []
                if m["text"]:
                    blocks.append({"type": "text", "text": m["text"]})
                for c in m["tool_calls"]:
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]})
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     **({"is_error": True} if r["is_error"] else {})}
                    for r in m["results"]]})
        return out

    async def generate(self, model, system, messages, tools) -> Turn:
        # Automatic prompt caching: each loop step re-sends the previous request plus
        # a little more, so the shared prefix is read from cache at ~1/10 the price.
        resp = await self.client.messages.create(
            model=model, max_tokens=16_000, system=system, tools=tools,
            messages=self._convert(messages), cache_control={"type": "ephemeral"})
        text = "\n".join(b.text for b in resp.content if b.type == "text" and b.text.strip())
        calls = [{"id": b.id, "name": b.name, "input": b.input} for b in resp.content if b.type == "tool_use"]
        u = resp.usage
        usage = {"input": u.input_tokens or 0, "output": u.output_tokens or 0,
                 "cache_read": u.cache_read_input_tokens or 0,
                 "cache_write": u.cache_creation_input_tokens or 0}
        # A turn stopped by max_tokens or a refusal can end inside a tool call, so never
        # run its tools; keep only the text so no tool_use is left without a result.
        if resp.stop_reason == "max_tokens":
            return Turn(text=f"{text}\n\n{CUT_OFF}".strip(), usage=usage)
        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            return Turn(text=f"{text}\n\n(Claude declined this request{f': {category}' if category else ''}.)".strip(),
                        usage=usage)
        if not text and not calls:
            return Turn(text="(Claude returned an empty reply.)", usage=usage)
        return Turn(text=text, tool_calls=calls, raw=(self.name, resp.content), usage=usage)


def _gemini_429(e) -> Exception:
    """Per-minute limits are worth waiting for (Gemini says how long); a daily quota isn't."""
    details = str(e.details)
    if "PerDay" in details:
        return RuntimeError("Gemini free-tier daily rate limit reached for this model. It resets at midnight "
                            "Pacific time; until then, switch models or use a paid key.")
    m = (re.search(r"retryDelay'?:\s*'?([\d.]+)s", details)
         or re.search(r"retry in ([\d.]+)\s*s", str(e), re.IGNORECASE))
    return RateLimited(f"Gemini rate limit: {e.message}", float(m.group(1)) if m else 30.0)


class GeminiProvider:
    name = "gemini"

    def __init__(self):
        from google import genai
        from google.genai import types
        # The SDK retries transient server errors (e.g. 503 "model overloaded", common on the
        # free tier). 429s are left to the agent loop, which waits as long as Gemini asks.
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=4, http_status_codes=[500, 502, 503, 504])))

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def _convert(self, messages):
        from google.genai import types
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append(types.Content(role="user", parts=[types.Part(text=m["text"])]))
            elif m["role"] == "assistant":
                if m.get("raw") and m["raw"][0] == self.name:
                    out.append(m["raw"][1])
                    continue
                parts = [types.Part(text=m["text"])] if m["text"] else []
                parts += [types.Part(function_call=types.FunctionCall(name=c["name"], args=c["input"]))
                          for c in m["tool_calls"]]
                out.append(types.Content(role="model", parts=parts))
            else:
                out.append(types.Content(role="user", parts=[
                    types.Part.from_function_response(
                        name=r["name"],
                        response={"error": r["content"]} if r["is_error"] else {"result": r["content"]})
                    for r in m["results"]]))
        return out

    async def generate(self, model, system, messages, tools) -> Turn:
        from google.genai import errors, types
        # Tools without arguments (git_status) get no schema: Gemini can reject empty "properties".
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                           parameters_json_schema=t["input_schema"] if t["input_schema"]["properties"] else None)
                 for t in tools]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=16_000)
        try:
            resp = await self.client.aio.models.generate_content(
                model=model, contents=self._convert(messages), config=config)
        except errors.APIError as e:
            if e.code == 429:
                raise _gemini_429(e) from e
            raise
        m = resp.usage_metadata
        cached = (m.cached_content_token_count or 0) if m else 0
        usage = {"input": ((m.prompt_token_count or 0) - cached) if m else 0,
                 "output": ((m.candidates_token_count or 0) + (m.thoughts_token_count or 0)) if m else 0,
                 "cache_read": cached, "cache_write": 0}
        if not resp.candidates or not resp.candidates[0].content:
            reason = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
            return Turn(text=f"(Gemini returned no content: {reason})", usage=usage)
        content = resp.candidates[0].content
        parts = content.parts or []
        text = "\n".join(p.text for p in parts if p.text and not p.thought and p.text.strip())
        calls = [{"id": fc.id or str(uuid.uuid4()), "name": fc.name, "input": dict(fc.args or {})}
                 for fc in (p.function_call for p in parts) if fc]
        if resp.candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
            return Turn(text=f"{text}\n\n{CUT_OFF}".strip(), usage=usage)  # a function call may be cut off: don't run it
        if not text and not calls:
            return Turn(text=f"(Gemini returned an empty reply: {resp.candidates[0].finish_reason})", usage=usage)
        return Turn(text=text, tool_calls=calls, raw=(self.name, content), usage=usage)


OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_PREFIX = "ollama/"          # model ids for local models look like "ollama/qwen2.5-coder:7b"
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))  # Ollama's own default (4096) would silently cut history
# OLLAMA_THINK=0 turns off Qwen3-style reasoning (much faster on CPU), =1 forces it on; unset = model default.
OLLAMA_THINK = {"0": False, "false": False, "1": True, "true": True}.get(os.environ.get("OLLAMA_THINK", "").lower())
OLLAMA_MAX_TOOL_CHARS = 8000      # small local models choke on huge tool results


def _ollama_installed() -> list[str]:
    """Names of locally installed Ollama models; [] if the server isn't running."""
    import json
    import urllib.request
    # A custom User-Agent: Cloudflare tunnels/proxies in front of a remote Ollama often block urllib's default one.
    req = urllib.request.Request(OLLAMA_HOST.rstrip("/") + "/api/tags", headers={"User-Agent": "orgai-backend/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # generous: a busy CPU-bound model answers slowly
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception as e:
        import logging
        logging.getLogger("uvicorn.error").warning("Can't list Ollama models at %s: %s", OLLAMA_HOST, e)  # shows in the uvicorn terminal
        return []


def _inline_tool_calls(text: str, tool_names: set):
    """Small models often write a tool call as JSON inside the message text instead of
    using the tool_calls field - alone, in a code fence, in <tool_call> tags, or after
    some prose. Find every {"name": <known tool>, "arguments": {...}} object in the text.
    Returns (calls, text_without_the_json)."""
    import json
    import re
    dec = json.JSONDecoder()
    calls, spans = [], []
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        args = obj.get("arguments", obj.get("parameters")) if isinstance(obj, dict) else None
        if isinstance(args, dict) and obj.get("name") in tool_names:
            calls.append({"id": str(uuid.uuid4()), "name": obj["name"], "input": args})
            spans.append((i, end))
            i = text.find("{", end)
        else:
            i = text.find("{", i + 1)
    if not calls:
        return [], text
    pieces, last = [], 0
    for start, end in spans:
        before = text[last:start]
        pieces.append(re.sub(r"(?:```(?:json)?|<tool_call>)\s*$", "", before))  # opening fence/tag
        after = re.match(r"\s*(?:```|</tool_call>)", text[end:])                # closing fence/tag
        last = end + (after.end() if after else 0)
    pieces.append(text[last:])
    return calls, "".join(pieces).strip()


class OllamaProvider:
    name = "ollama"

    def __init__(self):
        from ollama import AsyncClient
        self.client = AsyncClient(host=OLLAMA_HOST)
        self.last_stats: dict = {}  # timing of the latest call, read by the benchmark

    @staticmethod
    def available() -> bool:
        return bool(_ollama_installed())

    def _convert(self, system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m["text"]}
                if m["tool_calls"]:
                    msg["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["input"]}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            else:
                for r in m["results"]:
                    text = r["content"]
                    if len(text) > OLLAMA_MAX_TOOL_CHARS:
                        text = text[:OLLAMA_MAX_TOOL_CHARS] + "\n...(truncated)"
                    out.append({"role": "tool", "tool_name": r["name"], "content": text})
        return out

    async def generate(self, model, system, messages, tools) -> Turn:
        name = model.removeprefix(OLLAMA_PREFIX)
        specs = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["input_schema"]}} for t in tools]
        resp = await self.client.chat(
            model=name, messages=self._convert(system, messages), tools=specs, think=OLLAMA_THINK,
            options={"num_ctx": OLLAMA_NUM_CTX, "temperature": 0.2, "num_predict": 2048})
        msg = resp.message
        text = msg.content or ""
        calls = [{"id": str(uuid.uuid4()), "name": tc.function.name, "input": dict(tc.function.arguments or {})}
                 for tc in (msg.tool_calls or [])]
        inline = False
        if not calls:
            calls, text = _inline_tool_calls(text, {t["name"] for t in tools})
            inline = bool(calls)
        self.last_stats = {
            "gen_tokens": resp.eval_count or 0,
            "gen_seconds": (resp.eval_duration or 0) / 1e9,
            "prompt_tokens": resp.prompt_eval_count or 0,
            "prompt_seconds": (resp.prompt_eval_duration or 0) / 1e9,
            "inline_tool_call": inline,
        }
        usage = {"input": resp.prompt_eval_count or 0, "output": resp.eval_count or 0,
                 "cache_read": 0, "cache_write": 0}
        return Turn(text=text, tool_calls=calls, raw=None, usage=usage)


PROVIDERS = {"anthropic": AnthropicProvider, "gemini": GeminiProvider, "ollama": OllamaProvider}
_instances: dict = {}


def _catalog():
    models = [
        {"id": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "label": "Claude",
         "provider": "anthropic"},
        {"id": os.environ.get("GEMINI_MODEL", "gemini-3.4-flash-lite"), "label": "Gemini Flash-Lite",
         "provider": "gemini"},
    ]
    models += [{"id": OLLAMA_PREFIX + n, "label": f"Local: {n}", "provider": "ollama"}
               for n in _ollama_installed()]
    return models


def list_models():
    ollama_up = OllamaProvider.available()
    return [{**m, "available": ollama_up if m["provider"] == "ollama" else PROVIDERS[m["provider"]].available()}
            for m in _catalog()]


def default_model() -> str:
    models = list_models()
    return next((m for m in models if m["available"]), models[0])["id"]


def get_provider(model: str):
    """Route by model id; the provider is created lazily so a missing key for
    one provider doesn't break the other."""
    name = ("ollama" if model.startswith(OLLAMA_PREFIX)
            else "gemini" if model.startswith("gemini") else "anthropic")
    cls = PROVIDERS[name]
    if not cls.available():
        if name == "ollama":
            raise RuntimeError(f"Ollama isn't reachable at {OLLAMA_HOST} (needed for model '{model}'). "
                               "Start the Ollama app and try again.")
        key = "GEMINI_API_KEY" if name == "gemini" else "ANTHROPIC_API_KEY"
        raise RuntimeError(f"{key} is not set in agent-backend/.env (needed for model '{model}').")
    if name not in _instances:
        _instances[name] = cls()
    return _instances[name]
