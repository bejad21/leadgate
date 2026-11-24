"""A send Telegram refused must not look delivered, and a failed send must not put the bot token in a log."""
import logging
import traceback

import httpx
import pytest
import respx

from engine import telegram_client as tg

TOKEN = "987654:SECRET-token-value"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(tg.time, "sleep", waited.append)
    return waited


@respx.mock
def test_a_200_is_delivered():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    response = tg.send_message(42, "hello")

    assert response and response.status_code == 200


@respx.mock
@pytest.mark.parametrize("code", [403, 500, 502])
def test_a_refusal_or_server_fault_is_not_delivered(code):
    respx.post(URL).mock(return_value=httpx.Response(code, json={"ok": False, "description": "nope"}))

    assert not tg.send_message(42, "hello")


@respx.mock
def test_a_plain_text_retry_that_is_also_rejected_is_not_delivered():
    respx.post(URL).mock(return_value=httpx.Response(400))

    assert not tg.send_message(42, "hello")


@respx.mock
def test_a_429_waits_for_retry_after_and_retries_once(sleeps):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    response = tg.send_message(42, "hello")

    assert response and response.status_code == 200
    assert sleeps == [2]
    assert route.call_count == 2


@respx.mock
def test_the_429_wait_is_capped_at_five_seconds(sleeps):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"parameters": {"retry_after": 300}}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    assert tg.send_message(42, "hello")
    assert sleeps == [5]


@respx.mock
def test_a_second_429_is_not_delivered_and_is_not_retried_again(sleeps):
    route = respx.post(URL).mock(return_value=httpx.Response(429, json={"parameters": {"retry_after": 1}}))

    assert not tg.send_message(42, "hello")
    assert route.call_count == 2
    assert sleeps == [1]


@respx.mock
def test_a_429_without_retry_after_is_not_delivered_and_not_retried(sleeps):
    route = respx.post(URL).mock(return_value=httpx.Response(429, text="slow down"))

    assert not tg.send_message(42, "hello")
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_strict_failure_names_only_the_method_and_status():
    respx.post(URL).mock(return_value=httpx.Response(403, json={"ok": False}))

    with pytest.raises(tg.TelegramSendError) as caught:
        tg.send_message(42, "hello", strict=True)

    assert str(caught.value) == "Telegram sendMessage failed: HTTP 403"
    assert caught.value.status_code == 403
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@respx.mock
def test_strict_failure_leaves_no_token_in_the_traceback_or_the_log(caplog):
    respx.post(URL).mock(return_value=httpx.Response(403, json={"ok": False}))
    logger = logging.getLogger("engine.main")

    with caplog.at_level(logging.ERROR, logger="engine.main"):
        try:
            tg.send_message(42, "hello", strict=True)
        except Exception as exc:
            logger.exception("could not tell chat_id=42")
            rendered = "".join(traceback.format_exception(exc))

    assert "HTTP 403" in caplog.text
    for text in (caplog.text, rendered):
        assert TOKEN not in text
        assert "SECRET-token-value" not in text
        assert "api.telegram.org" not in text


@respx.mock
def test_strict_success_returns_the_response():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    response = tg.send_message(42, "hello", strict=True)
    assert response is not None and response.status_code == 200


@respx.mock
def test_strict_429_that_succeeds_on_retry_does_not_raise(sleeps):
    respx.post(URL).mock(
        side_effect=[httpx.Response(429, json={"parameters": {"retry_after": 1}}), httpx.Response(200, json={"ok": True})]
    )

    response = tg.send_message(42, "hello", strict=True)
    assert response is not None and response.status_code == 200


@respx.mock
def test_the_webhook_does_not_log_a_refused_reply_as_delivered(monkeypatch):
    """A 403 on the reply means the customer saw nothing, so the turn must not be logged as delivered."""
    from fastapi.testclient import TestClient

    import engine.main as main_module
    from engine.core.agent_loop import AgentTurnResult

    seen = {}
    monkeypatch.setattr(main_module, "_get_adapter", lambda: object())
    monkeypatch.setattr(main_module, "_get_llm_client", lambda: object())
    monkeypatch.setattr(main_module, "run_turn", lambda *a, **k: AgentTurnResult(reply="hi"))
    monkeypatch.setattr(main_module, "_after_reply", lambda chat_id, text, result, delivered, sender=None: seen.update(delivered=delivered))
    monkeypatch.setattr(main_module, "_seen_updates", main_module.OrderedDict())
    main_module.conversation_history.clear()
    respx.post(f"https://api.telegram.org/bot{main_module.config['TELEGRAM_BOT_TOKEN']}/sendMessage").mock(
        return_value=httpx.Response(403, json={"ok": False})
    )
    update = {"update_id": 9001, "message": {"message_id": 1, "chat": {"id": 31337, "type": "private"}, "text": "hello"}}

    response = TestClient(main_module.app).post(
        "/webhook/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": main_module.config["TELEGRAM_WEBHOOK_SECRET"]}
    )

    assert response.status_code == 200
    assert seen == {"delivered": False}


@respx.mock
@pytest.mark.parametrize("retry_after", ["-1", "NaN", "Infinity", "-Infinity", '"soon"', "null"])
def test_a_nonsense_retry_after_means_not_delivered_without_raising(sleeps, retry_after):
    route = respx.post(URL).mock(
        return_value=httpx.Response(429, content=('{"parameters": {"retry_after": %s}}' % retry_after).encode())
    )

    assert tg.send_message(42, "hello") is None
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_a_zero_retry_after_retries_straight_away(sleeps):
    respx.post(URL).mock(
        side_effect=[httpx.Response(429, json={"parameters": {"retry_after": 0}}), httpx.Response(200, json={"ok": True})]
    )

    assert tg.send_message(42, "hello")
    assert sleeps == [0]


@respx.mock
def test_an_absurdly_large_retry_after_is_not_delivered_without_raising(sleeps):
    huge = "9" * 400  # a JSON integer too big for a float
    route = respx.post(URL).mock(
        return_value=httpx.Response(429, content=('{"parameters": {"retry_after": %s}}' % huge).encode())
    )

    assert tg.send_message(42, "hello") is None
    assert route.call_count == 1
    assert sleeps == []
