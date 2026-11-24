import dataclasses
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str | None = None


@dataclass
class Usage:
    """Tokens a call used and what it cost. Zeros when the provider reports nothing."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # The provider's own figure when the response carries one, else the estimate from the configured
    # prices (see estimate_cost). None only on responses that never went through LLMClient.
    cost: float | None = None


@dataclass
class Attempt:
    """One model FailoverLLM asked (or passed over) and how it went: "ok", "cooldown" or an error kind."""

    model: str
    outcome: str


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    model: str = ""  # the model that produced this response, which after a failover is not the first one
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    attempts: list[Attempt] = field(default_factory=list)  # set by FailoverLLM


class ProviderError(Exception):
    """The provider answered HTTP 200 but without a usable completion (OpenRouter puts upstream
    faults in an `error` body this way). FailoverLLM treats it like a 5xx and tries the next model."""

# Key of the single-entry dict `_parse_args` returns for arguments it cannot use, so the agent loop
# can tell the model instead of crashing the turn.
INVALID_ARGUMENTS = "_invalid_arguments"


class ChatModel(Protocol):
    """What the agent loop needs from a model: one LLMClient, a FailoverLLM over several, or a test double."""

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


DEFAULT_TEMPERATURE = 0.0  # tool calling wants the most likely call, not a creative one
DEFAULT_MAX_TOKENS = 800
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 600.0
REASONING_EFFORTS = ("low", "medium", "high")
MAX_TOKENS_LIMIT = 1_000_000


def _number(name: str, raw: str, *, low: float = 0.0, high: float = math.inf) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        value = math.nan
    if not math.isfinite(value) or not low <= value <= high:
        bound = f"between {low:g} and {high:g}" if math.isfinite(high) else f"at least {low:g}"
        raise ValueError(f"{name} must be a number {bound}, got {raw!r}")
    return value


def _whole_number(name: str, raw: str, *, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not low <= value <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}, got {raw!r}")
    return value


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass(frozen=True)
class Sampling:
    """What every call sends besides the messages, and the prices that turn tokens into a cost."""

    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    seed: int | None = None  # sent only when set
    reasoning_effort: str | None = None  # low | medium | high, sent only when set (thinking models)
    price_per_mtok_input: float = 0.0  # per million tokens; 0 is right for free models
    price_per_mtok_output: float = 0.0
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS  # how long one model call may take; a free queue can need more

    @classmethod
    def from_env(cls) -> "Sampling":
        """Read when a client is built, after .env has been loaded. A bad value raises a ValueError
        that names the variable."""
        temperature = _env("LLM_TEMPERATURE")
        max_tokens = _env("LLM_MAX_TOKENS")
        seed = _env("LLM_SEED")
        effort = _env("LLM_REASONING_EFFORT").lower()
        if effort and effort not in REASONING_EFFORTS:
            raise ValueError(f"LLM_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}, got {effort!r}")
        price_in = _env("LLM_PRICE_PER_MTOK_INPUT")
        price_out = _env("LLM_PRICE_PER_MTOK_OUTPUT")
        timeout = _env("LLM_TIMEOUT_SECONDS")
        return cls(
            temperature=_number("LLM_TEMPERATURE", temperature, high=2.0) if temperature else DEFAULT_TEMPERATURE,
            max_tokens=(
                _whole_number("LLM_MAX_TOKENS", max_tokens, low=1, high=MAX_TOKENS_LIMIT)
                if max_tokens
                else DEFAULT_MAX_TOKENS
            ),
            seed=_whole_number("LLM_SEED", seed, low=0, high=2**31 - 1) if seed else None,
            reasoning_effort=effort or None,
            price_per_mtok_input=_number("LLM_PRICE_PER_MTOK_INPUT", price_in) if price_in else 0.0,
            price_per_mtok_output=_number("LLM_PRICE_PER_MTOK_OUTPUT", price_out) if price_out else 0.0,
            timeout_seconds=(
                _number("LLM_TIMEOUT_SECONDS", timeout, low=1.0, high=MAX_TIMEOUT_SECONDS)
                if timeout
                else DEFAULT_TIMEOUT_SECONDS
            ),
        )


def estimate_cost(usage: Usage, price_input: float, price_output: float) -> float:
    """The provider's cost if the response carried one, else tokens times the per-million-token prices."""
    if usage.cost is not None:
        return usage.cost
    return (usage.prompt_tokens * price_input + usage.completion_tokens * price_output) / 1_000_000


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _reported_cost(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _parse_usage(body, sampling: Sampling) -> Usage:
    """Never raises: a provider that omits `usage`, or sends something odd in it, gets zeros."""
    raw = body.get("usage") if isinstance(body, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    prompt, completion = _count(raw.get("prompt_tokens")), _count(raw.get("completion_tokens"))
    usage = Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=_count(raw.get("total_tokens")) or prompt + completion,
        cost=_reported_cost(raw.get("cost")),
    )
    usage.cost = estimate_cost(usage, sampling.price_per_mtok_input, sampling.price_per_mtok_output)
    return usage


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        sampling: Sampling | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.sampling = sampling if sampling is not None else Sampling.from_env()

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        return self._chat(messages, tools, None)

    def chat_requiring_a_tool(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """Like chat, but the model must call one of the tools instead of answering in words."""
        return self._chat(messages, tools, "required")

    def _chat(self, messages: list[dict], tools: list[dict], tool_choice: str | None) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.sampling.temperature,
            "max_tokens": self.sampling.max_tokens,
        }
        if tools:
            payload["tools"] = tools  # an empty list is left out: NVIDIA's API answers it with a 400
            if tool_choice:
                payload["tool_choice"] = tool_choice
        if self.sampling.temperature == 0:
            payload["top_p"] = 1  # some providers (mistral-large-4) reject greedy sampling without it
        if self.sampling.reasoning_effort:
            payload["reasoning_effort"] = self.sampling.reasoning_effort
        if self.sampling.seed is not None:
            payload["seed"] = self.sampling.seed
        started = time.monotonic()
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=self.sampling.timeout_seconds,
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        resp.raise_for_status()
        choice = _first_message(resp)
        raw_calls = choice.get("tool_calls") or []
        calls = [
            ToolCall(
                name=c["function"]["name"],
                arguments=_parse_args(c["function"].get("arguments")),
                id=c.get("id"),
            )
            for c in raw_calls
        ]
        return LLMResponse(
            content=_text_of(choice.get("content")),
            tool_calls=calls,
            model=self.model,
            usage=_parse_usage(resp.json(), self.sampling),
            latency_ms=latency_ms,
        )

def _text_of(content):
    """Plain text of a message. Some models send a list of blocks (text plus a reasoning block);
    keep the text blocks, drop the reasoning, and return None when no text is left."""
    if not isinstance(content, list):
        return content
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return text or None


def _first_message(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise ProviderError("the provider's answer was not JSON") from None
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
        error = body.get("error") if isinstance(body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else error
        raise ProviderError(f"the provider returned no completion: {str(detail or 'no choices')[:200]}")
    return choices[0]["message"]


def _parse_args(raw) -> dict:
    """Tool arguments as a dict. Providers send JSON text, which is empty for a tool with no
    arguments and can be cut off mid-way; anything unusable comes back as {INVALID_ARGUMENTS: start of it}."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {INVALID_ARGUMENTS: str(raw)[:200]}


# Statuses that mean "this model cannot answer right now" (retired, rate limited, provider fault).
# Anything else, such as 400, 401 or 403, is a problem with the request or key, and trying another
# model would only hide it.
_RETRYABLE = {404, 429} | set(range(500, 600))


def _error_kind(exc: Exception) -> str:
    """A short label for why a model could not answer, for the attempts list and the trace."""
    if isinstance(exc, ProviderError):
        return "provider_error"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "network_error"


def _worth_trying_elsewhere(exc: Exception) -> bool:
    if isinstance(exc, ProviderError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE
    return isinstance(exc, httpx.TransportError)  # timeouts, connection failures


def _attach_attempts(exc: Exception, attempts: list[Attempt]) -> None:
    """Keep the list of models tried on the error that ends the call, so a turn that failed still
    shows in its trace which models were asked and why each one failed."""
    exc.attempts = attempts  # type: ignore[attr-defined]


def _name_of(client) -> str:
    return getattr(client, "model", None) or type(client).__name__


class FailoverLLM:
    """Try an ordered list of models and remember which ones are down.

    Free model catalogs rotate: a model that answered yesterday can be gone today. Each model is
    tried in order; one that fails for a retryable reason is skipped for `cooldown_seconds` so a
    dead model costs one failed call, not one per message. If every model is cooling down they
    are all tried anyway, because waiting would only make things worse.

    The response lists every model asked or passed over (`attempts`) and names the one that
    answered (`model`), so a failover shows in the trace."""

    def __init__(self, clients: list, cooldown_seconds: float = 600, clock=time.monotonic):
        if not clients:
            raise ValueError("FailoverLLM needs at least one model")
        self.clients = list(clients)
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._down_until: dict[int, float] = {}

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        return self._ask(messages, tools, "chat")

    def chat_requiring_a_tool(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        # A model that cannot be told to call a tool is asked.
        return self._ask(messages, tools, "chat_requiring_a_tool")

    def _ask(self, messages: list[dict], tools: list[dict], method: str) -> LLMResponse:
        now = self._clock()
        ready = {i for i in range(len(self.clients)) if self._down_until.get(i, 0) <= now}
        tryable = ready or set(range(len(self.clients)))
        started = time.monotonic()
        attempts: list[Attempt] = []
        last_error: Exception | None = None
        for index, client in enumerate(self.clients):
            if index not in tryable:
                attempts.append(Attempt(_name_of(client), "cooldown"))
                continue
            try:
                response = getattr(client, method, client.chat)(messages, tools)
            except Exception as exc:
                attempts.append(Attempt(_name_of(client), _error_kind(exc)))
                if not _worth_trying_elsewhere(exc):
                    _attach_attempts(exc, attempts)
                    raise
                self._down_until[index] = self._clock() + self.cooldown_seconds
                last_error = exc
                continue
            model = response.model or _name_of(client)
            attempts.append(Attempt(model, "ok"))
            # A copy, so a response object that a test double hands out again is never changed.
            return dataclasses.replace(
                response, model=model, attempts=attempts, latency_ms=round((time.monotonic() - started) * 1000)
            )
        assert last_error is not None  # __init__ guarantees at least one client, so the loop ran
        _attach_attempts(last_error, attempts)
        raise last_error
