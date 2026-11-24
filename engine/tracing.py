"""One record per customer turn: which models answered, tokens, cost, timings, tool outcomes.

A trace says what happened, never what was said. It holds counts, names of models and tools, timings
and a keyed hash of the chat; it never holds a message, a reply, a tool argument or a result. That
rule is what makes it safe to keep next to the logs, so the fields below are the whole record and
nothing free-form is added to it.
"""
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)
# Separate from the module logger so the trace lines can be routed or silenced on their own.
trace_logger = logging.getLogger("leadgate.trace")

# How each tool call ended. `limit` covers the per-turn tool cap, the one-action rule and the
# per-chat write limit; `validation_error` covers everything wrong with the model's request.
TOOL_OUTCOMES = ("ok", "validation_error", "limit", "execution_error", "duplicate")
TURN_OUTCOMES = ("reply", "blocked", "error")

_file_lock = threading.Lock()


def prompt_version(prompt_text: str) -> str:
    """First 12 hex characters of the prompt's sha256, so a trace shows which prompt produced a reply."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]


def chat_ref(chat_id: int) -> str:
    """The keyed hash the dashboard mirror uses (HMAC with CHAT_REF_SECRET). Without the secret it is
    a plain truncated sha256, which hides the id from a casual reader but could be brute-forced, so
    set the secret anywhere traces leave the machine."""
    # Imported here: supabase_sync pulls in engine.leads, which imports the agent loop, which imports this module.
    from engine import supabase_sync

    try:
        return supabase_sync.chat_ref(chat_id)
    except supabase_sync.MissingSecret:
        return hashlib.sha256(str(chat_id).encode()).hexdigest()[:12]


# A message that asks for a person, as a count of how often customers want one. Only the yes or no is kept, never the words.
ASKS_FOR_A_PERSON = re.compile(
    r"\b(?:human|real person|a person|someone|salesperson|sales rep|agent|manager|representative|call me|phone me|ring me)\b",
    re.IGNORECASE,
)


@dataclass
class ToolTrace:
    name: str
    outcome: str
    duration_ms: int


@dataclass
class TurnTrace:
    domain: str = ""
    prompt_version: str = ""
    update_id: int | None = None
    chat_ref: str = ""
    models: list[str] = field(default_factory=list)  # the model that answered each LLM call
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0  # the whole turn
    llm_latency_ms: int = 0  # time spent waiting for the model
    tools: list[ToolTrace] = field(default_factory=list)
    retry_round_used: bool = False
    failover_used: bool = False
    source: str = "text"  # text the customer typed, or a button tap
    asked_for_person: bool = False  # the message asks for a human, a callback or an agent
    buttons_shown: int = 0  # buttons under the reply
    outcome: str = "reply"
    blocked_reason: str | None = None
    timestamp: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))

    def record_llm_call(self, response, elapsed_ms: int) -> None:
        """Add one model call from its response. Works for any response object, including a test
        double that carries no usage."""
        usage = response.usage
        self.llm_calls += 1
        self.models.append(response.model or "unknown")
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cost += usage.cost or 0.0
        self.llm_latency_ms += response.latency_ms or elapsed_ms
        if len(response.attempts) > 1:
            self.failover_used = True

    def record_failed_call(self, attempts: list, elapsed_ms: int) -> None:
        """A model call that raised. `attempts` is what FailoverLLM put on the error (empty for a plain
        client); nobody answered, so no model is added to `models`."""
        self.llm_calls += 1
        self.llm_latency_ms += elapsed_ms
        if len(attempts) > 1:
            self.failover_used = True

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["cost"] = round(self.cost, 8)
        return data


def check_trace_file() -> bool:
    """Called once at startup: is TRACE_FILE usable? A path the process cannot write (a relative path
    in a container image owned by root, say) would otherwise fail quietly on every turn. Returns
    whether traces will reach the file; with TRACE_FILE unset there is nothing to check."""
    path = (os.environ.get("TRACE_FILE") or "").strip()
    if not path:
        return True
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8"):
            pass
    except Exception as exc:
        logger.warning(
            "TRACE_FILE %s is not writable (%s); turn traces will only go to the log. "
            "Use a path the engine's user can write, for example one under /tmp in a container.",
            path,
            type(exc).__name__,
        )
        return False
    return True


def emit(trace: TurnTrace) -> None:
    """Write the trace as one JSON line to the `leadgate.trace` logger and, when TRACE_FILE is set,
    to that file. Never raises: a trace that cannot be written must not fail the customer's turn."""
    try:
        line = json.dumps(trace.to_dict(), separators=(",", ":"))
    except Exception:
        logger.exception("could not serialise a turn trace")
        return
    trace_logger.info(line)
    path = (os.environ.get("TRACE_FILE") or "").strip()
    if not path:
        return
    try:
        with _file_lock:  # handlers run in worker threads; one write at a time keeps lines whole
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception as exc:
        logger.warning("could not write the turn trace to TRACE_FILE (%s)", type(exc).__name__)
