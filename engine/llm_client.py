import httpx
from dataclasses import dataclass

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
