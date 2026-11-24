"""Structured JSON logging for the engine, set up once when the app starts.

Every line is one JSON object. The webhook binds the update id and a keyed hash of the chat
(`chat_ref`) at the start of a request, and every line logged while that request is handled carries
them, including lines from worker threads and background tasks, because the values live in
`contextvars`, which Starlette copies into the threads it uses.

Secrets are masked before a line is written: Telegram bot tokens (they sit in the URL of every Bot
API call), bearer tokens and `api_key=` style values, in messages and in exception text. The `httpx`
and `httpcore` loggers are held at WARNING because their INFO lines print the full request URL.
"""
import contextvars
import datetime as dt
import json
import logging
import os
import re
import sys
import traceback

DEFAULT_LEVEL = "INFO"
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}

_update_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("update_id", default=None)
_chat_ref: contextvars.ContextVar[str | None] = contextvars.ContextVar("chat_ref", default=None)

_REDACTIONS = [
    # https://api.telegram.org/bot<id>:<token>/method
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot<redacted>"),
    # a token on its own: <id>:<about 35 characters>; 30 or more is masked, to err on the side of hiding
    (re.compile(r"(?<![\w-])\d{5,}:[A-Za-z0-9_-]{30,}(?![\w-])"), "<redacted-token>"),
    # a bare provider key: OpenRouter (sk-or-v1-...) and the generic sk-... form, 20 or more characters
    (re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{20,}"), "<redacted-key>"),
    # the other providers this engine can use: NVIDIA (nvapi-), Mistral (mstrl_) and Google (AIzaSy)
    (re.compile(r"(?<![\w-])(?:nvapi-|mstrl_|AIzaSy)[A-Za-z0-9_-]{20,}"), "<redacted-key>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"""(api[_-]?key["']?\s*[=:]\s*["']?)[^\s&"',;}]+""", re.IGNORECASE), r"\1<redacted>"),
]

_HANDLER_MARK = "_leadgate_json_handler"
_FACTORY_MARK = "_leadgate_context_factory"


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def bind_request(update_id: int | None, chat_ref: str | None) -> None:
    """Make later log lines of this request (and the threads it starts) carry these two values."""
    _update_id.set(update_id)
    _chat_ref.set(chat_ref)


def resolve_log_level() -> int:
    raw = (os.environ.get("LOG_LEVEL") or "").strip()
    if not raw:
        return _LEVELS[DEFAULT_LEVEL]
    level = _LEVELS.get(raw.upper())
    if level is None:
        raise ValueError(f"LOG_LEVEL must be one of {', '.join(_LEVELS)}, got {raw!r}")
    return level


def _message(record: logging.LogRecord) -> str:
    """The formatted message. A log call with the wrong number of arguments would raise here, and a
    filter runs outside logging's own error handling, so fall back to the unformatted text."""
    try:
        return record.getMessage()
    except Exception:
        return str(record.msg)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(_message(record))
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = _message(record)
        entry: dict = {
            "timestamp": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        update_id = getattr(record, "update_id", None)
        chat_ref = getattr(record, "chat_ref", None)
        if update_id is not None:
            entry["update_id"] = update_id
        if chat_ref is not None:
            entry["chat_ref"] = chat_ref
        if record.name == "leadgate.trace":
            try:
                entry["trace"] = json.loads(message)
            except ValueError:
                pass
        if record.exc_info and not record.exc_text:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
        if record.exc_text:
            entry["exception"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False, default=str)


def _install_context_factory() -> None:
    """Stamp each record with the bound ids when it is created, so every handler sees them and a
    record made in a worker thread keeps the values of the request that started the thread."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, _FACTORY_MARK, False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.update_id = _update_id.get()
        record.chat_ref = _chat_ref.get()
        return record

    setattr(factory, _FACTORY_MARK, True)
    logging.setLogRecordFactory(factory)


def configure_logging(stream=None) -> None:
    """Install the JSON handler on the root logger. Called once at app startup, not at import, and
    safe to call again (it replaces its own handler). Raises ValueError for a bad LOG_LEVEL."""
    level = resolve_log_level()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level)
    _install_context_factory()
    # Their INFO lines print the request URL, and a Telegram Bot API URL contains the bot token.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
