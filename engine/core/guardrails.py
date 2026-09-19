"""Abuse protection for the tool-calling agent.

Domain-agnostic on purpose, like the rest of engine/core. The layers, in the
order a message meets them:

1. sanitize_user_text: strip characters and chat-template tokens that have no
   business in a customer message.
2. is_injection_attempt: cheap phrase heuristics. Easy to get around, so it is
   only a first filter; the checks below do not depend on it.
3. validate_tool_args: every tool argument the model produces is checked
   against that tool's own schema before anything touches Odoo.
4. filter_reply: the final reply cannot carry links or leak the system prompt.
"""
import math
import re
import unicodedata

MAX_MESSAGE = 2000
MAX_STR = 500
NOTES_MAX = 1000
NUM_MAX = 1_000_000_000
TOOL_CALL_CAP = 3
REPLY_MAX = 1500

INJECTION_REFUSAL = (
    "I can only help you browse the catalog and connect you with our team. "
    "What are you looking for?"
)
EMPTY_REPLY = "Sorry, I didn't catch that. Could you tell me what you're looking for?"
FALLBACK_REPLY = (
    "Sorry, I can't help with that. I can search the catalog for you or "
    "connect you with our team."
)

_TEMPLATE_TOKENS = re.compile(
    r"<\|[^|>]{0,40}\|>|\[/?INST\]|<</?SYS>>|</?s>|^#{1,4}\s*(system|assistant)\b:?",
    re.IGNORECASE | re.MULTILINE,
)

_INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules|prompt|guidelines|directives)\b",
    r"\b(reveal|show|print|repeat|display|leak|output|tell me)\b.{0,30}\b(system prompt|your (initial |original )?(instructions|prompt|rules)|hidden (prompt|instructions))",
    r"\bwhat (is|are) your (system prompt|instructions|rules)\b",
    r"\byou are now (?:an? |the )?(?:dan|unrestricted|unfiltered|jailbroken|free of|no longer|in (?:developer|debug|god) mode)\b",
    r"\bfrom now on,? you\b",
    r"\bpretend (to be|you are|you're)\b",
    r"\bact as (an? )?(unrestricted|unfiltered|jailbroken|dan|developer|admin|root|different)\b",
    r"\b(developer|debug|god|dan|jailbreak) mode\b",
    r"\bdo anything now\b",
    r"\bbypass\b.{0,30}\b(safety|filters?|rules|guardrails)\b",
    r"\bnew (system )?instructions?\s*:",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

# Links in every form a chat client would make tappable: schemes (including the
# "hxxp" defanging trick), mailto:, t.me, and bare domains such as evil.com/pay.
# A bare domain must not be preceded by a word character, "@", "." or "-", so
# the domain half of an email address the bot repeats back is left alone.
_URL_RE = re.compile(
    r"(?:(?:https?|hxxps?|ftp|tg)://\S+"
    r"|mailto:\S+"
    r"|www\.\S+"
    r"|t\.me/\S*"
    r"|(?<![\w@.-])[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|me|ly|co|app|dev|xyz|ru|info|biz|gl|link)\b(?:/\S*)?)",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """NFKC-normalise, drop control/format characters, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text)
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch in "\n\r\t":
            kept.append(" ")
        elif category in ("Cc", "Cf"):
            continue
        else:
            kept.append(ch)
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def sanitize_user_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _TEMPLATE_TOKENS.sub(" ", text)
    return _clean(text)[:MAX_MESSAGE]


def is_injection_attempt(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INJECTION_RE)


def _coerce_number(value, integer: bool):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("expected a number")
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError("expected a number") from exc
    if math.isnan(number) or math.isinf(number):
        raise ValueError("number out of range")
    if number < 0 or number > NUM_MAX:
        raise ValueError("number out of range")
    return int(round(number)) if integer else number


def validate_tool_args(tool_schema: dict, args: dict) -> dict:
    """Return a cleaned copy of `args` or raise ValueError.

    Driven by the tool's own JSON schema, so it protects every adapter without
    knowing anything about cars or property.
    """
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    parameters = tool_schema["function"]["parameters"]
    properties = parameters.get("properties", {})

    cleaned = {}
    for key, value in args.items():
        spec = properties.get(key)
        if spec is None or value is None:
            continue
        kind = spec.get("type")
        if kind in ("number", "integer"):
            cleaned[key] = _coerce_number(value, integer=(kind == "integer"))
        elif kind == "string":
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            limit = NOTES_MAX if key == "notes" else MAX_STR
            text = _clean(value)[:limit]
            if not text:
                continue
            if "enum" in spec and text not in spec["enum"]:
                continue
            cleaned[key] = text

    for key in parameters.get("required", []):
        if key not in cleaned:
            raise ValueError(f"missing required argument: {key}")
    return cleaned


def _leaks_prompt(reply: str, system_prompt: str) -> bool:
    window = 40
    normalised_reply = re.sub(r"\s+", " ", reply.lower())
    normalised_prompt = re.sub(r"\s+", " ", system_prompt.lower())
    for start in range(0, max(1, len(normalised_prompt) - window), 10):
        if normalised_prompt[start:start + window] in normalised_reply:
            return True
    return False


def filter_reply(reply: str, system_prompt: str) -> str:
    if not reply.strip():
        return EMPTY_REPLY
    if _leaks_prompt(reply, system_prompt):
        return FALLBACK_REPLY
    reply = _URL_RE.sub("[link removed]", reply)
    if len(reply) > REPLY_MAX:
        reply = reply[:REPLY_MAX - 3].rstrip() + "..."
    return reply
