import time
from dataclasses import dataclass

import httpx

@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str | None = None

@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]

class LLMClient:
    def __init__(self, api_key: str, model: str, base_url: str = "https://openrouter.ai/api/v1"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "tools": tools},
            timeout=30,
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]["message"]
        raw_calls = choice.get("tool_calls") or []
        calls = [
            ToolCall(
                name=c["function"]["name"],
                arguments=_parse_args(c["function"]["arguments"]),
                id=c.get("id"),
            )
            for c in raw_calls
        ]
        return LLMResponse(content=choice.get("content"), tool_calls=calls)

def _parse_args(raw: str) -> dict:
    import json
    return json.loads(raw)


# Statuses that mean "this model cannot answer right now" (retired, rate limited, provider fault).
# Anything else, such as 400, 401 or 403, is a problem with our request or key, and trying another
# model would only hide it.
_RETRYABLE = {404, 429} | set(range(500, 600))


def _worth_trying_elsewhere(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE
    return isinstance(exc, httpx.TransportError)  # timeouts, connection failures


class FailoverLLM:
    """Try an ordered list of models and remember which ones are down.

    Free model catalogs rotate: a model that answered yesterday can be gone today. Each model is
    tried in order; one that fails for a retryable reason is skipped for `cooldown_seconds` so a
    dead model costs one failed call, not one per message. If every model is cooling down they
    are all tried anyway, because waiting would only make things worse."""

    def __init__(self, clients: list, cooldown_seconds: float = 600, clock=time.monotonic):
        if not clients:
            raise ValueError("FailoverLLM needs at least one model")
        self.clients = list(clients)
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._down_until: dict[int, float] = {}

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        now = self._clock()
        ready = [i for i in range(len(self.clients)) if self._down_until.get(i, 0) <= now]
        last_error: Exception | None = None
        for index in ready or range(len(self.clients)):
            try:
                return self.clients[index].chat(messages, tools)
            except Exception as exc:
                if not _worth_trying_elsewhere(exc):
                    raise
                self._down_until[index] = self._clock() + self.cooldown_seconds
                last_error = exc
        raise last_error
