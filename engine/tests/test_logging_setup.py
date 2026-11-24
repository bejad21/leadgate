"""Structured JSON logs that carry the update and chat of the turn they belong to, and never a bot
token, bearer token or API key."""
import io
import json
import logging

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import engine.main as main_module
from engine import logging_setup
from engine.core.agent_loop import AgentTurnResult
from engine.main import app, config

FAKE_TOKEN = "123456789:AAH-fake_telegram_bot_token_value_35chars"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": config["TELEGRAM_WEBHOOK_SECRET"]}
SEND_URL = f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage"


@pytest.fixture
def logs(monkeypatch):
    """Install the engine's logging into a buffer, and put the process back as it was afterwards."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, logging.getLogRecordFactory())
    named = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    buffer = io.StringIO()

    def lines():
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    buffer.lines = lines  # type: ignore[attr-defined]
    logging_setup.configure_logging(stream=buffer)
    yield buffer
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    logging.setLogRecordFactory(saved[2])
    for name, level in named.items():
        logging.getLogger(name).setLevel(level)


def test_every_line_is_valid_json_with_the_basic_fields(logs):
    logging.getLogger("engine.test").warning("hello %s", "world")
    [line] = logs.lines()
    assert line["message"] == "hello world" and line["level"] == "WARNING" and line["logger"] == "engine.test"
    assert line["timestamp"].endswith("+00:00") or line["timestamp"].endswith("Z")
    assert "update_id" not in line and "chat_ref" not in line


def test_an_exception_is_included_as_text(logs):
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("engine.test").exception("it failed")
    [line] = logs.lines()
    assert "RuntimeError: boom" in line["exception"]


def test_configuring_twice_does_not_double_every_line(logs):
    logging_setup.configure_logging(stream=logs)
    logging.getLogger("engine.test").warning("once")
    assert len(logs.lines()) == 1


@pytest.mark.parametrize(
    "text",
    [
        f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage failed",
        f"token is {FAKE_TOKEN} in a bare message",
        "Authorization: Bearer sk-or-v1-abcdef0123456789",
        "calling with api_key=sk-secret-value-123 now",
        "calling with apikey: sk-secret-value-123 now",
        'body {"api_key": "sk-secret-value-123"}',
    ],
)
def test_secrets_are_masked_in_messages(logs, text):
    logging.getLogger("engine.test").error("%s", text)
    raw = logs.getvalue()
    for secret in (FAKE_TOKEN, "AAH-fake", "sk-or-v1-abcdef0123456789", "sk-secret-value-123"):
        assert secret not in raw
    assert "redacted" in raw


def test_secrets_are_masked_in_exception_text(logs):
    try:
        raise RuntimeError(f"Client error for url 'https://api.telegram.org/bot{FAKE_TOKEN}/getMe'")
    except RuntimeError:
        logging.getLogger("engine.test").exception("telegram call failed")
    assert "AAH-fake" not in logs.getvalue()


def test_a_message_without_secrets_is_untouched():
    assert logging_setup.redact("ratio 12:30 and bot commands") == "ratio 12:30 and bot commands"


def test_the_httpx_request_line_is_not_logged_at_the_default_level(logs):
    with respx.mock:
        respx.get(f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe").mock(return_value=httpx.Response(200, json={}))
        httpx.get(f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe")
    assert logs.getvalue() == "" or "AAH-fake" not in logs.getvalue()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_log_level_defaults_to_info_and_can_be_set(logs, monkeypatch):
    assert logging.getLogger().level == logging.INFO
    monkeypatch.setenv("LOG_LEVEL", "debug")
    logging_setup.configure_logging(stream=logs)
    assert logging.getLogger().level == logging.DEBUG


@pytest.mark.parametrize("value", ["loud", "10", "INFO!", "none"])
def test_a_bad_log_level_is_refused_and_names_the_variable(monkeypatch, value):
    monkeypatch.setenv("LOG_LEVEL", value)
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        logging_setup.resolve_log_level()


def test_trace_lines_keep_their_fields_at_the_top_level_of_the_json(logs):
    logging.getLogger("leadgate.trace").info(json.dumps({"update_id": 3, "outcome": "reply"}))
    [line] = logs.lines()
    assert line["logger"] == "leadgate.trace" and line["trace"] == {"update_id": 3, "outcome": "reply"}


def _update(text="Hello there"):
    return {
        "update_id": 77,
        "message": {"message_id": 1, "chat": {"id": 31337, "type": "private"}, "text": text},
    }


@respx.mock
def test_update_and_chat_ref_reach_log_lines_produced_in_a_worker_thread(logs, monkeypatch):
    from engine import tracing

    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    seen = {}

    def run_turn(history, adapter, llm, **kwargs):
        import threading

        seen["thread"] = threading.current_thread()
        logging.getLogger("engine.inside_the_turn").warning("working on it")
        return AgentTurnResult(reply="ok")

    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "run_turn", run_turn)
    monkeypatch.setattr(main_module, "_seen_updates", main_module.OrderedDict())

    response = TestClient(app).post("/webhook/telegram", json=_update(), headers=HEADERS)

    assert response.status_code == 200
    import threading

    assert seen["thread"] is not threading.main_thread()
    [inside] = [line for line in logs.lines() if line["logger"] == "engine.inside_the_turn"]
    assert inside["update_id"] == 77 and inside["chat_ref"] == tracing.chat_ref(31337)
    assert "31337" not in logs.getvalue()
    # the background step (the trace line) carries them too
    [trace_line] = [line for line in logs.lines() if line["logger"] == "leadgate.trace"]
    assert trace_line["update_id"] == 77 and trace_line["chat_ref"] == tracing.chat_ref(31337)


def test_a_request_without_context_does_not_inherit_the_last_one(logs):
    logging_setup.bind_request(5, "abc")
    logging_setup.bind_request(None, None)
    logging.getLogger("engine.test").warning("after")
    [line] = logs.lines()
    assert "update_id" not in line and "chat_ref" not in line


def test_startup_configures_logging_and_refuses_bad_settings(monkeypatch):
    import asyncio

    called = []
    monkeypatch.setattr(main_module.logging_setup, "configure_logging", lambda *a, **k: called.append(True))

    async def start():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(start())
    assert called == [True]

    monkeypatch.undo()
    monkeypatch.setenv("LLM_TEMPERATURE", "hot")
    with pytest.raises(ValueError, match="LLM_TEMPERATURE"):
        asyncio.run(start())


def test_a_log_call_with_wrong_format_arguments_never_raises_into_the_caller(logs):
    logging.getLogger("engine.test").info("two %s %s", "x")
    logging.getLogger("engine.test").warning("%d items", "many")
    lines = logs.lines()
    assert [line["message"] for line in lines] == ["two %s %s", "%d items"]


def test_a_malformed_log_call_still_has_its_secrets_masked(logs):
    logging.getLogger("engine.test").warning("Bearer sk-secret-token-value %s %s", "x")
    assert "sk-secret-token-value" not in logs.getvalue()


@pytest.mark.parametrize(
    "key",
    ["sk-or-v1-0123456789abcdef0123456789abcdef", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx", "sk-0123456789abcdefghij"],
)
def test_bare_provider_keys_are_masked(logs, key):
    logging.getLogger("engine.test").error("the model call failed with %s in it", key)
    assert key not in logs.getvalue() and "redacted" in logs.getvalue()


@pytest.mark.parametrize(
    "text",
    ["price 21,950.00 at 12:30", "desk-lamp and sk-short are fine", "task-2026-11-03-report-final-version"],
)
def test_ordinary_text_that_looks_a_little_like_a_key_is_left_alone(text):
    assert logging_setup.redact(text) == text


@respx.mock
def test_a_failed_turn_logs_the_chat_ref_and_never_the_raw_chat_id(logs, monkeypatch):
    from engine import tracing

    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    def run_turn(history, adapter, llm, **kwargs):
        raise RuntimeError("the model is down")

    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "run_turn", run_turn)
    monkeypatch.setattr(main_module, "_seen_updates", main_module.OrderedDict())

    response = TestClient(app).post("/webhook/telegram", json=_update(), headers=HEADERS)

    assert response.status_code == 200
    [failure] = [line for line in logs.lines() if "run_turn failed" in line["message"]]
    assert failure["chat_ref"] == tracing.chat_ref(31337) and failure["update_id"] == 77
    assert "31337" not in logs.getvalue()
    assert "chat_id" not in failure["message"]


def test_no_log_message_in_the_engine_names_the_raw_chat_id():
    """The bound chat_ref is on every record, so a message that also prints chat_id=<number> would let
    anyone holding the logs map each hash back to its Telegram id."""
    import re
    from pathlib import Path

    offenders = []
    for path in Path(main_module.__file__).parent.glob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"logger\.\w+\(.*chat_id\s*=\s*%s", line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == []


def test_nvidia_mistral_and_google_keys_are_masked_too():
    from engine.logging_setup import redact

    for key in ("nvapi-" + "a1B2" * 12, "mstrl_" + "x9Y8" * 10, "AIzaSy" + "q" * 33):
        masked = redact(f"provider said no for {key} just now")
        assert key not in masked and "<redacted-key>" in masked
    assert redact("the mstrl_ prefix alone and nvapi- are fine") == "the mstrl_ prefix alone and nvapi- are fine"
