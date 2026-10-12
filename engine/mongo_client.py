"""MongoDB conversation logging: one document per turn in the `conversations` collection.

Each document is `{chat_id, domain_type, message, reply, tool_calls, blocked, handled_by,
timestamp}`. `load_history` rebuilds a chat's recent history from it. The dashboard reads
Supabase, not this collection.

One MongoClient is created on first use and reused. A small pool suits one worker with bursty
traffic, and the 5s timeouts keep an unreachable Atlas cluster from stalling replies.
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
    """`tool_calls` is stored as plain dicts (name/arguments), not `ToolCall` instances, so the document is
    always BSON-serializable.
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
