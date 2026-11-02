"""Model Gateway: one interface over Anthropic and Gemini.

History is stored in a provider-neutral format so the model can be switched
between turns:
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_calls": [{"id","name","input"}], "raw": (provider, obj)|None}
  {"role": "tool", "results": [{"id","name","content","is_error"}]}
`raw` keeps the provider's own message (thinking blocks, thought signatures)
and is replayed verbatim when the same provider handles the next turn.
"""
import os
import uuid
from dataclasses import dataclass, field


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
        resp = await self.client.messages.create(
            model=model, max_tokens=4096, system=system, tools=tools,
            messages=self._convert(messages))
        text = "\n".join(b.text for b in resp.content if b.type == "text" and b.text.strip())
        calls = [{"id": b.id, "name": b.name, "input": b.input} for b in resp.content if b.type == "tool_use"]
        u = resp.usage
        usage = {"input": u.input_tokens or 0, "output": u.output_tokens or 0,
                 "cache_read": u.cache_read_input_tokens or 0,
                 "cache_write": u.cache_creation_input_tokens or 0}
        return Turn(text=text, tool_calls=calls, raw=(self.name, resp.content), usage=usage)


class GeminiProvider:
    name = "gemini"

    def __init__(self):
        from google import genai
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

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
        from google.genai import types
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                           parameters_json_schema=t["input_schema"]) for t in tools]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=4096)
        resp = await self.client.aio.models.generate_content(
            model=model, contents=self._convert(messages), config=config)
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
        return Turn(text=text, tool_calls=calls, raw=(self.name, content), usage=usage)


PROVIDERS = {"anthropic": AnthropicProvider, "gemini": GeminiProvider}
_instances: dict = {}


def _catalog():
    return [
        {"id": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "label": "Claude",
         "provider": "anthropic"},
        {"id": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"), "label": "Gemini Flash-Lite",
         "provider": "gemini"},
    ]


def list_models():
    return [{**m, "available": PROVIDERS[m["provider"]].available()} for m in _catalog()]


def default_model() -> str:
    models = list_models()
    return next((m for m in models if m["available"]), models[0])["id"]


def get_provider(model: str):
    """Route by model id; the provider is created lazily so a missing key for
    one provider doesn't break the other."""
    name = "gemini" if model.startswith("gemini") else "anthropic"
    cls = PROVIDERS[name]
    if not cls.available():
        key = "GEMINI_API_KEY" if name == "gemini" else "ANTHROPIC_API_KEY"
        raise RuntimeError(f"{key} is not set in agent-backend/.env (needed for model '{model}').")
    if name not in _instances:
        _instances[name] = cls()
    return _instances[name]
