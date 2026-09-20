"""MongoDB conversation-turn logging (Task 3.4).

Writes one document per conversation turn to MongoDB's `conversations`
collection: `{chat_id, domain_type, message, reply, tool_calls, timestamp}`.
This is the dataset Task 5.4's report and the dashboard's transcript view
both read.

Per the `mongodb-connection` skill's guidance: a single `MongoClient` is
created lazily on first use and reused for the lifetime of the process,
not re-created per call. This is a single-process, low-throughput
portfolio deployment (one FastAPI worker, bursty conversational traffic,
not a high-QPS service), so the pool is intentionally small rather than
the driver's 100-connection default:

- `maxPoolSize=10`: comfortably above any realistic concurrent-webhook
  count for a single-worker deployment, without holding open connections
  the workload will never use.
- `minPoolSize=0`: no pre-warming: conversation turns are bursty, not a
  steady stream, so idle connections would just sit unused between messages.
- `serverSelectionTimeoutMS=5000` / `connectTimeoutMS=5000`: fail fast
  (5s) rather than hang the webhook response if MongoDB (Atlas free tier)
  is briefly unreachable -- logging a turn should never make the customer
  wait a long time for their reply.
"""
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

_client: MongoClient | None = None


def _get_client(mongodb_uri: str) -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(
            mongodb_uri,
            maxPoolSize=10,
            minPoolSize=0,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
    return _client


def load_history(mongodb_uri: str, chat_id: int, limit_turns: int = 20) -> list[dict]:
    """Rebuild a chat's recent conversation from the turns already logged.

    Returns user/assistant messages, oldest first. The tool-call messages of
    earlier turns are not replayed: the assistant reply already states what the
    tool returned, which is all the model needs to follow a conversation.
    Turns the guardrails blocked are left out, so an injection attempt cannot
    steer later turns through a restart.
    """
    collection = _get_client(mongodb_uri)["leadgate"]["conversations"]
    newest_first = (
        collection.find({"chat_id": chat_id, "blocked": {"$ne": True}, "handled_by": {"$ne": "human"}})
        .sort("timestamp", -1)
        .limit(limit_turns)
    )
    history: list[dict] = []
    for doc in reversed(list(newest_first)):
        history.append({"role": "user", "content": doc["message"]})
        history.append({"role": "assistant", "content": doc["reply"]})
    return history


def log_turn(
    mongodb_uri: str,
    chat_id: int,
    domain_type: str,
    message: str,
    reply: str,
    tool_calls: list[Any],
    blocked: bool = False,
    handled_by: str | None = None,
) -> None:
    """Write one conversation-turn document to MongoDB's `conversations`
    collection in the `leadgate` database.

    `tool_calls` is stored as a list of plain dicts (name/arguments), not
    raw `ToolCall` dataclass instances, so the document is always
    JSON/BSON-serializable regardless of what the caller passes in.
    """
    client = _get_client(mongodb_uri)
    db = client["leadgate"]

    serialized_tool_calls = [
        {"name": getattr(tc, "name", None), "arguments": getattr(tc, "arguments", None)}
        if not isinstance(tc, dict)
        else tc
        for tc in tool_calls
    ]

    db["conversations"].insert_one(
        {
            "chat_id": chat_id,
            "domain_type": domain_type,
            "message": message,
            "reply": reply,
            "tool_calls": serialized_tool_calls,
            "blocked": blocked,
            "handled_by": handled_by,
            "timestamp": datetime.now(timezone.utc),
        }
    )
