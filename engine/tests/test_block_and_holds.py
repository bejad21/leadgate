"""Holding up the inventory: one active hold per Telegram account, and a Block button for the owner."""
from collections.abc import Callable
from typing import Any

from engine import notifier, owner_bot
from engine.adapters.cars import CarsAdapter
from engine.core.actions import CUSTOMER_REF
from engine.core.agent_loop import run_turn
from engine.leads import LeadInfo
from engine.llm_client import LLMResponse, ToolCall
from engine.tests.fakes import OWNER, FakeOdoo, item, make_world, press, say
from engine.tests.test_agent_habits import ShopAdapter, says
from engine.tests.test_agent_loop import FakeLLMClient
from engine.tracing import chat_ref

# make_world builds its object on the fly, so it is typed as anything here.
new_world: Callable[[], Any] = make_world


def cars_adapter():
    return CarsAdapter(FakeOdoo(items={8: item(8)}))


def test_a_hold_carries_the_accounts_reference_so_odoo_can_allow_one_per_account():
    adapter = cars_adapter()
    token = CUSTOMER_REF.set("ref-abc")
    try:
        adapter.execute_tool("reserve_item", {"item_id": 8, "customer_name": "Sam", "customer_contact": "sam@example.com"})
    finally:
        CUSTOMER_REF.reset(token)
    reserve = next(kwargs for _, method, _, kwargs in adapter.odoo.calls if method == "action_reserve")
    assert reserve["holder_chat"] == "ref-abc"


def test_a_turn_gives_its_tools_the_chats_reference_and_takes_it_back():
    seen = []

    class Watching(ShopAdapter):
        def execute_tool(self, name, args):
            seen.append(CUSTOMER_REF.get())
            return super().execute_tool(name, args)

    llm = FakeLLMClient([LLMResponse(content=None, tool_calls=[ToolCall(name="search_inventory", arguments={}, id="a")]), says("ok")])
    run_turn([{"role": "user", "content": "any cars?"}], Watching(), llm, chat_id=555)
    assert seen == [chat_ref(555)] and CUSTOMER_REF.get() is None


def lead(**extra):
    return LeadInfo(lead_id=71, domain_type="cars", item_name="Corolla", customer_name="Sarah", email=None, phone=None,
                    price=None, price_verified=True, kind="reservation", **extra)


def test_every_alert_with_a_known_chat_has_a_block_button():
    labels = [[b["text"] for b in row] for row in notifier.alert_keyboard(lead())["inline_keyboard"]]
    assert ["Block customer"] in labels
    without = [b["text"] for row in notifier.alert_keyboard(lead(), can_reply=False)["inline_keyboard"] for b in row]
    assert "Block customer" not in without


def world_with_holds():
    world = new_world()
    world.odoo.items[8].update(status="reserved", reserved_chat=chat_ref(555), reservation_lead_id=90)
    return world


def test_block_stops_the_chat_releases_its_holds_and_closes_the_lead():
    world = world_with_holds()
    press(world, "mute:71")
    assert world.store.is_blocked(555) and not world.store.is_blocked(556)
    assert world.odoo.items[7]["status"] == "available" and world.odoo.items[8]["status"] == "available"
    assert (71, "lost") in world.rec.statuses
    assert world.rec.toasts[-1][1].startswith("Blocked.") and "/unblock 71" in world.rec.toasts[-1][1]


def test_unblock_lets_the_customer_write_again():
    world = world_with_holds()
    press(world, "mute:71")
    say(world, "/unblock 71")
    assert not world.store.is_blocked(555)
    assert "can write to the assistant again" in world.rec.owner_texts[-1]["text"]


def test_unblock_says_so_when_there_is_nothing_to_undo():
    world = new_world()
    say(world, "/unblock 71")
    assert "not blocked" in world.rec.owner_texts[-1]["text"]
    say(world, "/unblock soon")
    assert "/unblock" in world.rec.owner_texts[-1]["text"]


def test_a_lead_without_a_known_chat_cannot_be_blocked():
    world = new_world()
    press(world, "mute:999")
    assert "cannot" in world.rec.toasts[-1][1]


def test_only_the_owner_can_block():
    world = new_world()
    press(world, "mute:71", sender=OWNER + 1, chat=OWNER + 1)
    assert not world.store.is_blocked(555)


def test_a_button_from_a_removed_feature_never_blocks():
    world = new_world()
    press(world, "b:71")
    assert not world.store.is_blocked(555)


def test_the_help_names_unblock():
    assert "/unblock" in owner_bot.HELP
