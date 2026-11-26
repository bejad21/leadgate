"""Several engine workers, a block list, a deletion period for chat text and encryption of the engine's own tables.

- Two workers must not run one chat at the same time, and each must see what the other did, search results included.
- A chat the owner blocked is ignored.
- Chat text older than CHAT_TEXT_RETENTION_DAYS is deleted.
- With CHAT_ENCRYPTION_KEY set, the engine's tables hold no readable chat text.
"""
import asyncio
import json
import time

import pytest
from cryptography.fernet import Fernet

from engine import store as store_module
from engine.core.agent_loop import AgentTurnResult
from engine.main import conversation_history
from engine.store import InMemoryStore, SupabaseStore, text_retention_days
from engine.tests.fake_postgrest import FakePostgrest
from engine.tests.test_main_buttons import CHAT, XT5, keyboard_data, sent, setup, tap, telegram, typed  # noqa: F401
from engine.tests.test_store_contract import store, unique  # noqa: F401

# What every store must do (the same three implementations as the other contract tests)


def test_only_one_owner_holds_a_chat_until_it_lets_go(store):  # noqa: F811
    chat = unique()
    assert store.claim_chat(chat, "worker-a", 60) is True
    assert store.claim_chat(chat, "worker-b", 60) is False
    assert store.claim_chat(chat, "worker-a", 60) is True  # renewing its own claim
    store.release_chat(chat, "worker-b")  # not the holder: nothing happens
    assert store.claim_chat(chat, "worker-b", 60) is False
    store.release_chat(chat, "worker-a")
    assert store.claim_chat(chat, "worker-b", 60) is True


def test_a_claim_lapses_so_a_dead_worker_cannot_hold_a_chat_for_ever(store):  # noqa: F811
    chat = unique()
    assert store.claim_chat(chat, "worker-a", 1) is True
    time.sleep(1.2)
    assert store.claim_chat(chat, "worker-b", 60) is True


def test_simultaneous_claims_of_a_chat_have_one_winner(store):  # noqa: F811
    import threading

    chat, results, go = unique(), [], threading.Event()

    def claim(name):
        go.wait()
        results.append(store.claim_chat(chat, name, 60))

    threads = [threading.Thread(target=claim, args=(f"w{n}",)) for n in range(10)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1


def test_a_chats_state_is_saved_and_read_back(store):  # noqa: F811
    chat = unique()
    assert store.load_state(chat) is None
    store.save_state(chat, '[{"role": "user", "content": "héllo"}]')
    store.save_state(chat, '[{"role": "user", "content": "again"}]')
    assert store.load_state(chat) == '[{"role": "user", "content": "again"}]'
    assert store.claim_chat(chat, "worker-a", 60) is True and store.load_state(chat) is not None


def test_a_blocked_chat_stays_blocked_until_the_lead_is_unblocked(store):  # noqa: F811
    chat, lead = unique(), unique()
    assert store.is_blocked(chat) is False
    store.block_chat(chat, lead)
    store.block_chat(chat, lead)
    assert store.is_blocked(chat) is True
    assert store.unblock_lead(lead) is True and store.is_blocked(chat) is False
    assert store.unblock_lead(lead) is False


# The deletion period


def test_the_deletion_period_defaults_to_ninety_days_and_zero_keeps_everything(monkeypatch):
    monkeypatch.delenv("CHAT_TEXT_RETENTION_DAYS", raising=False)
    assert text_retention_days() == 90
    monkeypatch.setenv("CHAT_TEXT_RETENTION_DAYS", "0")
    assert text_retention_days() == 0


@pytest.mark.parametrize("bad", ["-1", "ninety", "1.5"])
def test_a_bad_deletion_period_is_refused(monkeypatch, bad):
    monkeypatch.setenv("CHAT_TEXT_RETENTION_DAYS", bad)
    with pytest.raises(ValueError, match="CHAT_TEXT_RETENTION_DAYS"):
        text_retention_days()


def test_old_chat_text_is_deleted_and_recent_text_is_kept(monkeypatch):
    now = [1_000_000.0]
    memory = InMemoryStore(clock=lambda: now[0])
    monkeypatch.setenv("CHAT_TEXT_RETENTION_DAYS", "30")
    memory.log_turn(1, "cars", "old", "old", [])
    memory.save_state(1, "[]")
    now[0] += 31 * 86400
    memory.log_turn(2, "cars", "new", "new", [])
    memory.save_state(2, "[]")
    memory.purge_expired()
    assert memory.load_history(1) == [] and memory.load_state(1) is None
    assert memory.load_history(2) and memory.load_state(2) == "[]"


def test_the_database_purge_is_told_the_deletion_period(monkeypatch):
    fake = FakePostgrest()
    monkeypatch.setenv("CHAT_TEXT_RETENTION_DAYS", "45")
    SupabaseStore("https://proj.supabase.co", "k", client=fake.client()).purge_expired()
    assert json.loads(fake.requests[-1].content)["p_text_days"] == 45


# Encryption of the engine's tables


def encrypted_store(fake, key):
    from engine.crypto import TextCipher

    return SupabaseStore("https://proj.supabase.co", "k", client=fake.client(), cipher=TextCipher(key) if key else None)


def test_no_readable_chat_text_reaches_the_engines_tables():
    fake = FakePostgrest()
    secret_store = encrypted_store(fake, Fernet.generate_key().decode())
    secret_store.log_turn(1, "cars", "my phone is 5135550188", "Thanks Hugo", [{"name": "x", "arguments": {"phone": "5135550188"}}])
    secret_store.save_state(1, '[{"role": "user", "content": "my phone is 5135550188"}]')
    stored = json.dumps(fake.tables["chat_turns"]) + json.dumps(fake.tables["chat_state"])
    assert "5135550188" not in stored and "Hugo" not in stored and "enc1:" in stored
    assert secret_store.load_history(1)[0]["content"] == "my phone is 5135550188"
    assert "5135550188" in (secret_store.load_state(1) or "")


def test_text_written_before_the_key_was_set_is_still_read():
    fake = FakePostgrest()
    encrypted_store(fake, None).log_turn(1, "cars", "plain q", "plain a", [])
    assert [m["content"] for m in encrypted_store(fake, Fernet.generate_key().decode()).load_history(1)] == ["plain q", "plain a"]


def test_text_that_cannot_be_read_is_left_out_rather_than_shown_garbled():
    fake = FakePostgrest()
    first = encrypted_store(fake, Fernet.generate_key().decode())
    first.log_turn(1, "cars", "q", "a", [])
    first.save_state(1, "[]")
    for other in (encrypted_store(fake, Fernet.generate_key().decode()), encrypted_store(fake, None)):
        assert other.load_history(1) == [] and other.load_state(1) is None


def test_a_bad_key_stops_the_engine_and_names_the_setting(monkeypatch):
    from engine.crypto import TextCipher

    monkeypatch.setenv("CHAT_ENCRYPTION_KEY", "not-a-key")
    with pytest.raises(ValueError, match="CHAT_ENCRYPTION_KEY"):
        TextCipher.from_env()
    monkeypatch.setenv("CHAT_ENCRYPTION_KEY", "  ")
    assert TextCipher.from_env() is None


# The webhook with a shared store


@pytest.fixture
def fresh_store():
    memory = InMemoryStore()
    store_module.set_store(memory)
    yield memory
    store_module.set_store(None)


def test_a_turn_saves_the_chat_with_its_search_results(telegram, fresh_store, setup):  # noqa: F811
    def turn(history, adapter, llm, **kwargs):
        history.append({"role": "tool", "tool_call_id": "t", "name": "search_inventory", "content": json.dumps({"matches": [XT5]})})
        history.append({"role": "assistant", "content": "The XT5, $40,850."})
        return AgentTurnResult(reply="The XT5, $40,850.")

    import engine.main as main_module

    main_module.run_turn = turn  # replaced again by the fixture's monkeypatch teardown
    typed("a cadillac")
    saved = json.loads(fresh_store.load_state(CHAT))
    assert [m["role"] for m in saved] == ["user", "tool", "assistant"]


def test_a_worker_picks_up_what_another_worker_saved(telegram, fresh_store, setup):  # noqa: F811
    other = [{"role": "user", "content": "from the other worker"}, {"role": "assistant", "content": "answered there"}]
    conversation_history[CHAT] = [{"role": "user", "content": "stale copy"}, {"role": "assistant", "content": "old"}]
    fresh_store.save_state(CHAT, json.dumps(other))
    typed("and the next thing")
    seen = [m["content"] for m in setup[1].calls[0]["history"] if m["role"] == "user"]
    assert seen == ["from the other worker", "and the next thing"]


def test_a_turn_waits_for_the_worker_that_holds_the_chat(telegram, fresh_store, setup, monkeypatch):  # noqa: F811
    import engine.main as main_module

    monkeypatch.setattr(main_module, "CHAT_LEASE_POLL_SECONDS", 0.05)
    assert fresh_store.claim_chat(CHAT, "other-worker", 1)
    started = time.monotonic()
    typed("hello")
    assert time.monotonic() - started >= 0.8 and len(setup[1].calls) == 1
    assert fresh_store.claim_chat(CHAT, "other-worker", 1)  # let go after the turn


def test_a_store_that_cannot_lock_or_save_still_answers_the_customer(telegram, fresh_store, setup, monkeypatch):  # noqa: F811
    def down(*args, **kwargs):
        raise store_module.StoreUnavailable("down")

    for name in ("claim_chat", "release_chat", "load_state", "save_state"):
        monkeypatch.setattr(fresh_store, name, down)
    typed("hello", update_id=1)
    typed("again", update_id=2)
    assert len(setup[1].calls) == 2
    assert [m["content"] for m in setup[1].calls[1]["history"] if m["role"] == "user"] == ["hello", "again"]


def test_several_workers_are_refused_without_a_shared_store(monkeypatch, fresh_store):
    import engine.main as main_module

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        asyncio.run(main_module.lifespan(None).__aenter__())


# The block list


def test_a_blocked_chat_is_ignored_typed_or_tapped(telegram, fresh_store, setup):  # noqa: F811
    fresh_store.block_chat(CHAT, 71)
    assert typed("hello", update_id=1).status_code == 200
    assert tap("act:hold:315", update_id=2).status_code == 200
    assert setup[1].calls == [] and not telegram["send"].called


def test_other_chats_are_not_affected_by_a_block(telegram, fresh_store, setup):  # noqa: F811
    fresh_store.block_chat(CHAT + 1, 71)
    typed("hello")
    assert len(setup[1].calls) == 1
