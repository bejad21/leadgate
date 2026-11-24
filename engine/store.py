"""The engine's durable state, behind one small interface.

What it holds:
- each chat's recent conversation, so a restart does not make the assistant forget the customer
- which Telegram chat a lead came from (a lead is in Odoo; the chat id is not, and the copy in
  Supabase is a one-way hash), and which alert message belongs to which lead, so the owner can answer
- which update ids were already handled, so a redelivery does not run a turn twice
- the rate-limit counters, and the per-lead flags

SupabaseStore keeps all of it in Postgres through Supabase's REST interface (migrations/0003).
InMemoryStore is the same interface in a dict: tests use it, and so does an engine started with no
SUPABASE_URL (it forgets everything on restart, and says so in the log).

Anything that must happen once is a single statement that decides atomically (an insert that ignores
duplicates, an increment that returns the new count). Nothing reads first and writes after.
"""
import logging
import os
import threading
import time
from typing import Any, Protocol

import httpx

from engine import supabase_rest

logger = logging.getLogger(__name__)

LEAD_CHAT_RETENTION_DAYS = 180
UPDATE_RETENTION_DAYS = 7
REQUEST_TIMEOUT_SECONDS = 4.0
# After a failed call the store is not tried again for this long, so an outage costs each customer
# nothing beyond the first failure instead of one timeout per call.
UNAVAILABLE_COOLDOWN_SECONDS = 10.0


class StoreUnavailable(RuntimeError):
    """The store could not be reached or refused the request. The message names no URL or key."""


class Store(Protocol):
    def ping(self) -> None:
        """Return if the store answers; raise otherwise. The readiness check calls this."""
        ...

    def load_history(self, chat_id: int, limit_turns: int = 20) -> list[dict]:
        """The chat's last `limit_turns` turns as user/assistant messages, oldest first. Turns the
        guardrails blocked are left out, so an injection attempt cannot steer later turns."""
        ...

    def log_turn(
        self, chat_id: int, domain_type: str, message: str, reply: str, tool_calls: list[Any], blocked: bool = False
    ) -> None: ...

    def claim_update(self, source: str, update_id: int) -> bool:
        """Record that this update id is being handled. True for the first caller, False for any other."""
        ...

    def hit_rate_limit(self, key: str, window_seconds: int, max_hits: int) -> bool:
        """Count one hit for `key` in the current fixed window. True while the count is within `max_hits`."""
        ...

    def save_lead_chat(
        self, lead_id: int, *, chat_id: int, kind: str, item_name: str, customer_name: str,
        detail: str | None = None, username: str | None = None,
    ) -> None: ...

    def get_lead_chat(self, lead_id: int) -> dict | None: ...

    def add_alert_message(self, lead_id: int, message_id: int) -> None: ...

    def lead_for_message(self, message_id: int) -> int | None: ...

    def has_flag(self, lead_id: int, flag: str) -> bool: ...

    def set_flag(self, lead_id: int, flag: str) -> None: ...

    def purge_expired(self) -> None:
        """Delete state that is no longer needed: old update ids, finished rate-limit windows, and
        lead chats older than LEAD_CHAT_RETENTION_DAYS."""
        ...


def _tool_call_dicts(tool_calls: list[Any]) -> list[dict]:
    return [
        tc if isinstance(tc, dict) else {"name": getattr(tc, "name", None), "arguments": getattr(tc, "arguments", None)}
        for tc in tool_calls
    ]


class InMemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._turns: list[dict] = []
        self._updates: set[tuple[str, int]] = set()
        self._windows: dict[tuple[str, int], int] = {}
        self._leads: dict[int, dict] = {}
        self._messages: dict[int, int] = {}
        self._flags: set[tuple[int, str]] = set()

    def ping(self) -> None:
        """Memory is always there."""

    def load_history(self, chat_id, limit_turns=20):
        with self._lock:
            mine = [t for t in self._turns if t["chat_id"] == chat_id and not t["blocked"]]
        history: list[dict] = []
        for turn in mine[-limit_turns:]:
            history.append({"role": "user", "content": turn["message"]})
            history.append({"role": "assistant", "content": turn["reply"]})
        return history

    def log_turn(self, chat_id, domain_type, message, reply, tool_calls, blocked=False):
        with self._lock:
            self._turns.append(
                {"chat_id": chat_id, "domain_type": domain_type, "message": message, "reply": reply,
                 "tool_calls": _tool_call_dicts(tool_calls), "blocked": blocked}
            )

    def claim_update(self, source, update_id):
        with self._lock:
            if (source, update_id) in self._updates:
                return False
            self._updates.add((source, update_id))
            return True

    def hit_rate_limit(self, key, window_seconds, max_hits):
        window = int(time.time() // window_seconds)
        with self._lock:
            self._windows = {k: v for k, v in self._windows.items() if k[0] != key or k[1] >= window}
            count = self._windows[(key, window)] = self._windows.get((key, window), 0) + 1
        return count <= max_hits

    def save_lead_chat(self, lead_id, *, chat_id, kind, item_name, customer_name, detail=None, username=None):
        with self._lock:
            previous = self._leads.get(lead_id, {})
            self._leads[lead_id] = {
                "lead_id": lead_id, "chat_id": chat_id, "kind": kind, "item_name": item_name,
                "customer_name": customer_name, "detail": detail, "username": username or previous.get("username"),
            }

    def get_lead_chat(self, lead_id):
        with self._lock:
            record = self._leads.get(lead_id)
            return dict(record) if record else None

    def add_alert_message(self, lead_id, message_id):
        with self._lock:
            self._messages[message_id] = lead_id

    def lead_for_message(self, message_id):
        with self._lock:
            return self._messages.get(message_id)

    def has_flag(self, lead_id, flag):
        with self._lock:
            return (lead_id, flag) in self._flags

    def set_flag(self, lead_id, flag):
        with self._lock:
            self._flags.add((lead_id, flag))

    def purge_expired(self) -> None:
        """Nothing to expire: memory is gone at the next restart."""


class SupabaseStore:
    """The Store in Postgres, over Supabase's REST interface with the service key.

    Plain httpx (already a dependency) is enough: every operation is one request, and the two that must
    decide atomically are an insert that ignores duplicates and a database function. The tables have
    row level security on and no policy, so only the service key can touch them.
    """

    def __init__(self, base_url: str, service_key: str, *, client: httpx.Client | None = None, clock=time.monotonic):
        self._rest = f"{base_url.rstrip('/')}/rest/v1"
        self._headers = supabase_rest.service_headers(service_key)
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._clock = clock
        self._down_until = 0.0

    @classmethod
    def from_env(cls) -> "SupabaseStore | None":
        """A store for the configured project, or None when SUPABASE_URL or the key is not set.
        Raises ValueError if SUPABASE_URL is set but is not an address."""
        base = supabase_rest.base_url()
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not base or not key:
            return None
        return cls(base, key)

    def _call(self, method: str, path: str, *, params=None, body=None, prefer: str | None = None, probe: bool = False):
        """One request. Raises StoreUnavailable on any failure; a recent failure short-circuits (except `probe`)."""
        if not probe and self._clock() < self._down_until:
            raise StoreUnavailable("the store was unreachable a moment ago")
        headers = dict(self._headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            response = self._client.request(method, f"{self._rest}/{path}", params=params, json=body, headers=headers)
        except httpx.HTTPError as exc:
            self._down_until = self._clock() + UNAVAILABLE_COOLDOWN_SECONDS
            raise StoreUnavailable(f"could not reach the store ({type(exc).__name__})") from None
        if response.status_code >= 400:
            if response.status_code >= 500:
                self._down_until = self._clock() + UNAVAILABLE_COOLDOWN_SECONDS
            raise StoreUnavailable(f"the store answered HTTP {response.status_code}")
        self._down_until = 0.0
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def ping(self) -> None:
        self._call("GET", "lead_chats", params={"select": "lead_id", "limit": "1"}, probe=True)

    def load_history(self, chat_id, limit_turns=20):
        rows = self._call(
            "GET", "chat_turns",
            params={"chat_id": f"eq.{chat_id}", "blocked": "eq.false", "select": "message,reply",
                    "order": "id.desc", "limit": str(limit_turns)},
        )
        history: list[dict] = []
        for row in reversed(rows or []):
            history.append({"role": "user", "content": row["message"]})
            history.append({"role": "assistant", "content": row["reply"]})
        return history

    def log_turn(self, chat_id, domain_type, message, reply, tool_calls, blocked=False):
        self._call(
            "POST", "chat_turns", prefer="return=minimal",
            body={"chat_id": chat_id, "domain_type": domain_type, "message": message, "reply": reply,
                  "tool_calls": _tool_call_dicts(tool_calls), "blocked": blocked},
        )

    def claim_update(self, source, update_id):
        rows = self._call(
            "POST", "processed_updates", params={"on_conflict": "source,update_id"},
            prefer="resolution=ignore-duplicates,return=representation",
            body={"source": source, "update_id": update_id},
        )
        return bool(rows)

    def hit_rate_limit(self, key, window_seconds, max_hits):
        answer = self._call(
            "POST", "rpc/rate_limit_hit",
            body={"p_key": key, "p_window_seconds": window_seconds, "p_max": max_hits},
        )
        return answer is True

    def save_lead_chat(self, lead_id, *, chat_id, kind, item_name, customer_name, detail=None, username=None):
        body = {"lead_id": lead_id, "chat_id": chat_id, "kind": kind, "item_name": item_name,
                "customer_name": customer_name, "detail": detail}
        if username:
            body["username"] = username  # left out when absent, so a stored username is kept
        self._call(
            "POST", "lead_chats", params={"on_conflict": "lead_id"},
            prefer="resolution=merge-duplicates,return=minimal", body=body,
        )

    def get_lead_chat(self, lead_id):
        rows = self._call(
            "GET", "lead_chats",
            params={"lead_id": f"eq.{lead_id}", "limit": "1",
                    "select": "lead_id,chat_id,kind,item_name,customer_name,detail,username"},
        )
        return rows[0] if rows else None

    def add_alert_message(self, lead_id, message_id):
        self._call(
            "POST", "alert_messages", params={"on_conflict": "message_id"},
            prefer="resolution=merge-duplicates,return=minimal", body={"message_id": message_id, "lead_id": lead_id},
        )

    def lead_for_message(self, message_id):
        rows = self._call("GET", "alert_messages", params={"message_id": f"eq.{message_id}", "select": "lead_id", "limit": "1"})
        return rows[0]["lead_id"] if rows else None

    def has_flag(self, lead_id, flag):
        rows = self._call(
            "GET", "lead_flags", params={"lead_id": f"eq.{lead_id}", "flag": f"eq.{flag}", "select": "lead_id", "limit": "1"}
        )
        return bool(rows)

    def set_flag(self, lead_id, flag):
        self._call(
            "POST", "lead_flags", params={"on_conflict": "lead_id,flag"},
            prefer="resolution=ignore-duplicates,return=minimal", body={"lead_id": lead_id, "flag": flag},
        )

    def purge_expired(self) -> None:
        self._call(
            "POST", "rpc/purge_expired",
            body={"p_update_days": UPDATE_RETENTION_DAYS, "p_lead_chat_days": LEAD_CHAT_RETENTION_DAYS},
        )


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    """The process's store, built on first use: Supabase when it is configured, memory otherwise."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                built = SupabaseStore.from_env()
                if built is None:
                    logger.warning(
                        "SUPABASE_URL or SUPABASE_SERVICE_KEY is not set; conversations and owner replies use memory and reset on restart"
                    )
                _store = built or InMemoryStore()
    return _store


def set_store(store: Store | None) -> None:
    """Swap the store (tests use this to stay away from a real database)."""
    global _store
    _store = store
