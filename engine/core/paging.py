"""The next page of a search: what the "Next 5" button under a long list needs.

A search shows at most PAGE_SIZE items and says how many match. The button carries only the offset and a short key of
the search it belongs to, so a button left over from an older search is recognised and refused. The search itself is
read back from the chat's history, so nothing extra is stored.
"""
import hashlib
import json

PAGE_SIZE = 5
# How an adapter is told which page to return. It is not a field of any argument model, so the model cannot set it:
# validation drops it, and only the engine adds it afterwards.
OFFSET_ARGUMENT = "_offset"


def _calls(history: list[dict]):
    """(call id, tool name, arguments) of every tool call in the history, newest first."""
    for message in reversed(history):
        for call in reversed(message.get("tool_calls") or []):
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                continue
            if isinstance(arguments, dict):
                yield call.get("id"), function.get("name"), arguments


def _results(history: list[dict]) -> dict:
    results = {}
    for message in history:
        if message.get("role") == "tool":
            try:
                results[message.get("tool_call_id")] = json.loads(message.get("content") or "")
            except ValueError:
                continue
    return results


def search_key(name: str, arguments: dict) -> str:
    text = json.dumps([name, arguments], sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def latest_search(history: list[dict], write_tools) -> tuple[str, dict, int] | None:
    """(tool name, arguments as the model gave them, match count) of the chat's latest search that answered."""
    results = _results(history)
    for call_id, name, arguments in _calls(history):
        result = results.get(call_id)
        if not isinstance(name, str) or name in write_tools or not isinstance(result, dict) or "matches" not in result:
            continue
        count = result.get("count")
        return name, arguments, count if isinstance(count, int) else len(result["matches"])
    return None


def next_page(history: list[dict], write_tools, offset: int) -> dict | None:
    """What the next-page button carries, or None when the latest search has nothing beyond `offset`."""
    found = latest_search(history, write_tools)
    if found is None:
        return None
    name, arguments, count = found
    return {"offset": offset, "key": search_key(name, arguments)} if count > offset else None
