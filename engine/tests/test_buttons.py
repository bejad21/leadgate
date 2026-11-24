"""The buttons themselves: what is on them, what a tap may say, and what a forged tap cannot do."""
import datetime as dt
import json

import httpx
import pytest
import respx

from engine import buttons
from engine.core.agent_loop import customer_gave_day_and_time
from engine.telegram_client import answer_callback_query, clear_keyboard, extract_callback, send_message

XT5 = {
    "id": 315, "name": "2024 Cadillac XT5", "price": 40850.0,
    "attributes": json.dumps({"mileage": "16797", "condition": "Used", "location": "Cincinnati, OH"}),
}
HOUSE = {
    "id": 372, "name": "3bd/1ba residential - East Hampton, Connecticut", "price": 450000.0,
    "attributes": json.dumps({"bed": "3", "bath": "1", "house_size": "2014", "city": "East Hampton", "state": "Connecticut"}),
}


def keyboard_for(rows):
    keyboard = buttons.results_keyboard(rows)
    assert keyboard is not None
    return keyboard


def all_data(keyboard):
    return [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]


# Results


def test_each_item_gets_a_button_with_its_name_and_price():
    rows = [XT5, {"id": 140, "name": "2024 Cadillac Lyriq", "price": 39780.0, "attributes": "{}"}]
    keyboard = keyboard_for(rows)
    labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
    assert labels == ["2024 Cadillac XT5 · $40,850", "2024 Cadillac Lyriq · $39,780"]
    assert all_data(keyboard) == ["pick:315", "pick:140"]


def test_one_item_goes_straight_to_its_actions():
    assert all_data(buttons.results_keyboard([XT5])) == ["act:hold:315", "act:view:315", "act:call:315"]


def test_no_items_no_buttons_and_at_most_five():
    assert buttons.results_keyboard([]) is None
    many = [{"id": n, "name": f"Car {n}", "price": 1000.0 + n} for n in range(1, 9)]
    assert len(keyboard_for(many)["inline_keyboard"]) == 5


def test_a_long_name_is_cut_to_fit_on_a_button():
    label = buttons.item_label({"name": "x" * 200, "price": 1000.0})
    assert len(label) <= buttons.LABEL_MAX and label.endswith("…")


def test_every_callback_fits_telegrams_64_bytes():
    today = dt.date(2026, 11, 21)
    data = [*all_data(buttons.actions_keyboard(999999999999)), *all_data(buttons.day_keyboard(999999999999, today)),
            *all_data(buttons.slot_keyboard(999999999999, "2026-11-28"))]
    assert data and all(len(d.encode()) <= 64 for d in data)


# The item card


def test_a_car_card_shows_price_miles_condition_and_place():
    assert buttons.item_card(XT5) == "2024 Cadillac XT5\n$40,850 · 16,797 miles · Used · Cincinnati, OH"


def test_a_home_card_shows_price_beds_baths_size_and_place():
    assert buttons.item_card(HOUSE) == (
        "3bd/1ba residential - East Hampton, Connecticut\n$450,000 · 3 bed · 1 bath · 2,014 sq ft · East Hampton, Connecticut"
    )


def test_a_row_with_odd_attributes_still_gets_a_card():
    assert buttons.item_card({"name": "Thing", "price": 5.0, "attributes": "not json"}) == "Thing\n$5"


# Days and times


def test_the_day_buttons_start_today_and_name_the_next_days():
    keyboard = buttons.day_keyboard(315, dt.date(2026, 11, 21))  # a Saturday
    labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
    assert labels == ["Today", "Tomorrow", "Mon 23 Nov", "Tue 24 Nov", "Wed 25 Nov", "Thu 26 Nov"]
    assert all_data(keyboard)[0] == "day:315:2026-11-21"
    assert all(len(row) <= 2 for row in keyboard["inline_keyboard"])


def test_the_time_buttons_are_the_three_slots_the_booking_tool_accepts():
    keyboard = buttons.slot_keyboard(315, "2026-11-28")
    assert all_data(keyboard) == ["slot:315:2026-11-28:morning", "slot:315:2026-11-28:afternoon", "slot:315:2026-11-28:evening"]


# Reading a tap


@pytest.mark.parametrize(
    "data, expected",
    [
        ("pick:315", ("pick", 315, [])),
        ("act:hold:315", ("act", 315, ["hold"])),
        ("act:view:7", ("act", 7, ["view"])),
        ("act:call:7", ("act", 7, ["call"])),
        ("day:315:2026-11-28", ("day", 315, ["2026-11-28"])),
        ("slot:315:2026-11-28:morning", ("slot", 315, ["2026-11-28", "morning"])),
    ],
)
def test_the_taps_this_module_makes_are_read_back(data, expected):
    assert buttons.parse_callback(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        None, 5, "", "pick", "pick:", "pick:abc", "pick:-1", "pick:3.5", "pick:315:extra", "pick:1234567890123",
        "act:sell:315", "act:hold", "act:hold:x", "day:315:tomorrow", "day:315:2026-13-45", "slot:315:2026-11-28:midnight",
        "slot:315:2026-11-28", "day:315:٢٠٢٦-11-28", "ADMIN:1", "act:hold:315;drop table", "x" * 200, "pick:٣١٥",
    ],
)
def test_anything_else_is_refused(data):
    assert buttons.parse_callback(data) is None


# What a tap says for the customer


def test_hold_and_call_taps_are_sentences_the_customer_could_have_typed():
    assert buttons.customer_words("act", XT5, ["hold"]) == "Please hold the 2024 Cadillac XT5 for me."
    assert buttons.customer_words("act", XT5, ["call"]) == "Please have someone call me about the 2024 Cadillac XT5."


def test_a_viewing_tap_names_the_weekday_the_date_and_the_part_of_the_day():
    words = buttons.customer_words("slot", XT5, ["2026-11-28", "morning"])
    assert words == "I'd like to see the 2024 Cadillac XT5 on Saturday 2026-11-28 in the morning."
    assert customer_gave_day_and_time(words)  # the viewing guard accepts it as the customer's own day and time


def test_taps_that_only_navigate_say_nothing():
    assert buttons.customer_words("pick", XT5, []) is None
    assert buttons.customer_words("act", XT5, ["view"]) is None
    assert buttons.customer_words("day", XT5, ["2026-11-28"]) is None


# The Telegram calls


@respx.mock
def test_send_message_puts_the_keyboard_in_the_request(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    route = respx.post("https://api.telegram.org/bott/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    keyboard = buttons.actions_keyboard(315)
    send_message(42, "hello", reply_markup=keyboard)
    assert json.loads(route.calls.last.request.content)["reply_markup"] == keyboard


@respx.mock
def test_the_plain_text_retry_keeps_the_keyboard(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    route = respx.post("https://api.telegram.org/bott/sendMessage").mock(
        side_effect=[httpx.Response(400, json={"ok": False}), httpx.Response(200, json={"ok": True})]
    )
    keyboard = buttons.actions_keyboard(315)
    assert send_message(42, "a <broken", reply_markup=keyboard) is not None
    retry = json.loads(route.calls.last.request.content)
    assert "parse_mode" not in retry and retry["reply_markup"] == keyboard


@respx.mock
def test_a_message_without_buttons_has_no_reply_markup(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    route = respx.post("https://api.telegram.org/bott/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    send_message(42, "hello")
    assert "reply_markup" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_answer_callback_query_and_clear_keyboard_post_the_right_calls(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    answer = respx.post("https://api.telegram.org/bott/answerCallbackQuery").mock(return_value=httpx.Response(200, json={"ok": True}))
    clear = respx.post("https://api.telegram.org/bott/editMessageReplyMarkup").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert answer_callback_query("cb1") is True and json.loads(answer.calls.last.request.content) == {"callback_query_id": "cb1"}
    assert clear_keyboard(42, 7) is True
    assert json.loads(clear.calls.last.request.content) == {"chat_id": 42, "message_id": 7, "reply_markup": {"inline_keyboard": []}}


@respx.mock
def test_a_failing_answer_or_clear_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    respx.post("https://api.telegram.org/bott/answerCallbackQuery").mock(side_effect=httpx.ConnectError("down"))
    respx.post("https://api.telegram.org/bott/editMessageReplyMarkup").mock(return_value=httpx.Response(400, json={"ok": False}))
    assert answer_callback_query("cb1") is False
    assert clear_keyboard(42, 7) is False


def test_extract_callback_reads_a_tap_and_ignores_everything_else():
    update = {
        "update_id": 9,
        "callback_query": {
            "id": "cb1", "from": {"id": 5, "username": "sam_buyer", "first_name": "Sam"}, "data": "pick:315",
            "message": {"message_id": 70, "chat": {"id": 42, "type": "private"}},
        },
    }
    tap = extract_callback(update)
    assert tap is not None and tap["sender"] is not None
    assert tap["id"] == "cb1" and tap["chat_id"] == 42 and tap["message_id"] == 70 and tap["data"] == "pick:315"
    assert tap["sender"]["username"] == "sam_buyer"
    assert extract_callback({"message": {"text": "hi"}}) is None
    assert extract_callback({"callback_query": {"id": "x", "data": "pick:1"}}) is None  # no chat to answer in
    assert extract_callback({"callback_query": {"id": 5, "data": "pick:1", "message": {"chat": {"id": 1}}}}) is None
    assert extract_callback({"callback_query": {"id": "x", "data": None, "message": {"chat": {"id": 1}}}}) is None
