"""The small amount of state the owner channel needs.

- Which Telegram chat a lead came from (a lead is in Odoo; the chat id is not, and the
  copy in Supabase is a one-way hash), so the owner can answer the customer.
- Which alert message belongs to which lead, so "reply to this alert" means something.
- Which chats are in human mode, where the assistant stays quiet and the owner talks.

It lives in MongoDB next to the conversation log, so it survives a restart. Without a
MongoDB URI an in-memory copy is used, which is fine for trying things out but forgets
everything on restart.
"""
import datetime as dt
import logging
import os

logger = logging.getLogger(__name__)

DB_NAME = "leadgate"
LEAD_CHAT_RETENTION_DAYS = 180


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class InMemoryStore:
    def __init__(self):
        self._leads: dict[int, dict] = {}
        self._messages: dict[int, int] = {}
        self._handoffs: dict[int, dict] = {}
        self._flags: set[tuple[int, str]] = set()

    def has_flag(self, lead_id, flag):
        return (lead_id, flag) in self._flags

    def set_flag(self, lead_id, flag):
        self._flags.add((lead_id, flag))

    def save_lead_chat(self, lead_id, *, chat_id, kind, item_name, customer_name, detail=None):
        self._leads[lead_id] = {
            "lead_id": lead_id, "chat_id": chat_id, "kind": kind,
            "item_name": item_name, "customer_name": customer_name, "detail": detail,
        }

    def get_lead_chat(self, lead_id):
        return self._leads.get(lead_id)

    def add_alert_message(self, lead_id, message_id):
        self._messages[message_id] = lead_id

    def lead_for_message(self, message_id):
        return self._messages.get(message_id)

    def set_handoff(self, chat_id, lead_id, *, until):
        self._handoffs[chat_id] = {"chat_id": chat_id, "lead_id": lead_id, "until": until}

    def get_handoff(self, chat_id, now=None):
        record = self._handoffs.get(chat_id)
        if record and record["until"] > (now or _utcnow()):
            return record
        return None

    def clear_handoff(self, chat_id):
        self._handoffs.pop(chat_id, None)


class MongoStore:
    def __init__(self, uri: str):
        from engine.mongo_client import _get_client

        self._db = _get_client(uri)[DB_NAME]
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Look-ups by lead and by alert message happen on every button press, so they are
        indexed. Old records expire on their own: a handoff at its end time, a lead's chat
        record after LEAD_CHAT_RETENTION_DAYS, so customer chat ids are not kept for ever."""
        try:
            leads, handoffs = self._db["lead_chats"], self._db["handoffs"]
            leads.create_index("lead_id", unique=True)
            leads.create_index("alert_message_ids")
            leads.create_index("created", expireAfterSeconds=LEAD_CHAT_RETENTION_DAYS * 86400)
            handoffs.create_index("chat_id", unique=True)
            handoffs.create_index("until", expireAfterSeconds=0)
        except Exception:
            logger.exception("could not create the store's indexes; it works without them, more slowly")

    def has_flag(self, lead_id, flag):
        return self._db["lead_chats"].count_documents({"lead_id": lead_id, flag: True}, limit=1) > 0

    def set_flag(self, lead_id, flag):
        self._db["lead_chats"].update_one(
            {"lead_id": lead_id}, {"$set": {flag: True}, "$setOnInsert": {"created": _utcnow()}}, upsert=True
        )

    def save_lead_chat(self, lead_id, *, chat_id, kind, item_name, customer_name, detail=None):
        self._db["lead_chats"].update_one(
            {"lead_id": lead_id},
            {
                "$set": {"chat_id": chat_id, "kind": kind, "item_name": item_name, "customer_name": customer_name, "detail": detail},
                "$setOnInsert": {"created": _utcnow(), "alert_message_ids": []},
            },
            upsert=True,
        )

    def get_lead_chat(self, lead_id):
        # a record with no chat id (made only to trace an alert message) is not a chat
        return self._db["lead_chats"].find_one({"lead_id": lead_id, "chat_id": {"$exists": True}}, {"_id": 0})

    def add_alert_message(self, lead_id, message_id):
        # upsert, so a message is traceable even if the lead's chat was not saved
        self._db["lead_chats"].update_one(
            {"lead_id": lead_id}, {"$addToSet": {"alert_message_ids": message_id}, "$setOnInsert": {"created": _utcnow()}}, upsert=True
        )

    def lead_for_message(self, message_id):
        doc = self._db["lead_chats"].find_one({"alert_message_ids": message_id}, {"lead_id": 1})
        return doc["lead_id"] if doc else None

    def set_handoff(self, chat_id, lead_id, *, until):
        self._db["handoffs"].update_one({"chat_id": chat_id}, {"$set": {"lead_id": lead_id, "until": until}}, upsert=True)

    def get_handoff(self, chat_id, now=None):
        doc = self._db["handoffs"].find_one({"chat_id": chat_id}, {"_id": 0})
        if not doc:
            return None
        until = doc["until"]
        if until.tzinfo is None:  # Mongo returns naive UTC datetimes
            until = until.replace(tzinfo=dt.timezone.utc)
        return doc if until > (now or _utcnow()) else None

    def clear_handoff(self, chat_id):
        self._db["handoffs"].delete_one({"chat_id": chat_id})


_store = None


def get_store():
    global _store
    if _store is None:
        uri = os.environ.get("MONGODB_URI")
        if uri:
            _store = MongoStore(uri)
        else:
            logger.warning("MONGODB_URI is not set; owner replies and human mode use memory and reset on restart")
            _store = InMemoryStore()
    return _store


def set_store(store) -> None:
    """Swap the store (tests use this to stay away from a real database)."""
    global _store
    _store = store
