"""A small stand-in for Supabase's REST interface, enough for SupabaseStore.

It understands the requests the store makes (equality filters, order, limit, upsert with
`on_conflict`, `Prefer: resolution=ignore-duplicates` and the two functions) and nothing more. The
store contract tests run SupabaseStore against it, so the store's real request building and response
reading are exercised without a network. It is the same shape the live run checks against the real
service.
"""
import json
import threading
import time

import httpx

PRIMARY_KEYS = {
    "lead_chats": ["lead_id"],
    "alert_messages": ["message_id"],
    "processed_updates": ["source", "update_id"],
    "lead_flags": ["lead_id", "flag"],
    "chat_state": ["chat_id"],
    "blocked_chats": ["chat_id"],
}


class FakePostgrest:
    def __init__(self):
        names = ("chat_turns", "lead_chats", "alert_messages", "processed_updates", "lead_flags", "chat_state", "blocked_chats")
        self.tables: dict[str, list[dict]] = {name: [] for name in names}
        self.claims: dict[int, tuple[str, float]] = {}
        self.catalog: dict[int, dict] = {}
        self.windows: dict[tuple[str, int], int] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: Exception | int | None = None
        self._lock = threading.Lock()
        self._next_id = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.fail_with, Exception):
            raise self.fail_with
        if isinstance(self.fail_with, int):
            return httpx.Response(self.fail_with, json={"message": "boom"})
        if request.headers.get("apikey") in (None, "") or not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"message": "no key"})
        path = request.url.path.split("/rest/v1/", 1)[1]
        with self._lock:
            if path.startswith("rpc/"):
                return self._rpc(path[4:], json.loads(request.content))
            if request.method == "GET":
                return httpx.Response(200, json=self._select(path, request.url.params))
            if request.method == "POST":
                return self._insert(path, request)
            if request.method == "DELETE":
                gone = self._select(path, request.url.params)
                keys = PRIMARY_KEYS[path]
                self.tables[path] = [r for r in self.tables[path] if not any(all(r[k] == g[k] for k in keys) for g in gone)]
                return httpx.Response(200, json=gone)
        return httpx.Response(405)

    def _select(self, table: str, params) -> list[dict]:
        rows = list(self.tables[table])
        for column, expr in params.items():
            if column in ("select", "order", "limit"):
                continue
            assert expr.startswith("eq."), expr
            wanted = expr[3:]
            rows = [r for r in rows if str(r.get(column)).lower() == wanted.lower()]
        if params.get("order") == "id.desc":
            rows = sorted(rows, key=lambda r: r["id"], reverse=True)
        rows = rows[: int(params["limit"])] if "limit" in params else rows
        columns = params.get("select", "*").split(",")
        return [r if columns == ["*"] else {c: r.get(c) for c in columns} for r in rows]

    def _insert(self, table: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prefer = request.headers.get("prefer", "")
        key = PRIMARY_KEYS.get(table)
        existing = None
        if key:
            existing = next((r for r in self.tables[table] if all(r.get(k) == body.get(k) for k in key)), None)
        if existing is not None:
            if "ignore-duplicates" in prefer:
                return httpx.Response(201, json=[] if "return=representation" in prefer else None)
            existing.update(body)  # merge-duplicates
            return httpx.Response(201)
        row = dict(body)
        if table == "chat_turns":
            self._next_id += 1
            row["id"] = self._next_id
        self.tables[table].append(row)
        if "return=representation" in prefer:
            return httpx.Response(201, json=[row])
        return httpx.Response(201)

    def _rpc(self, name: str, args: dict) -> httpx.Response:
        if name == "rate_limit_hit":
            window = int(time.time() // args["p_window_seconds"])
            slot = (args["p_key"], window)
            self.windows[slot] = self.windows.get(slot, 0) + 1
            return httpx.Response(200, json=self.windows[slot] <= args["p_max"])
        if name == "apply_catalog_event":
            return httpx.Response(200, json=self._apply_catalog_event(args))
        if name == "claim_chat":
            holder = self.claims.get(args["p_chat"])
            if holder and holder[0] != args["p_owner"] and holder[1] > time.monotonic():
                return httpx.Response(200, json=False)
            self.claims[args["p_chat"]] = (args["p_owner"], time.monotonic() + args["p_seconds"])
            return httpx.Response(200, json=True)
        if name == "release_chat":
            if self.claims.get(args["p_chat"], ("",))[0] == args["p_owner"]:
                del self.claims[args["p_chat"]]
            return httpx.Response(204)
        if name == "purge_expired":
            return httpx.Response(204)
        return httpx.Response(404)

    def _apply_catalog_event(self, args: dict) -> str:
        """The same rules as migrations/0004: insert, else update unless older, else unchanged or stale."""
        written = args["p_written_at"]  # ISO 8601 with +00:00, so strings compare in time order
        fields = {"domain_type": args["p_domain_type"], "name": args["p_name"], "price": args["p_price"], "status": args["p_status"]}
        row = self.catalog.get(args["p_odoo_id"])
        if row is None:
            self.catalog[args["p_odoo_id"]] = {**fields, "updated_at": written}
            return "inserted"
        same = all(row[k] == v for k, v in fields.items())
        if row["updated_at"] < written or (row["updated_at"] == written and not same):
            row.update(fields, updated_at=written)
            return "updated"
        return "unchanged" if row["updated_at"] == written else "stale"
