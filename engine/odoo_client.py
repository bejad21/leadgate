import threading
import xmlrpc.client


class OdooClient:
    """Thin wrapper around Odoo's XML-RPC API for common/object endpoints."""

    def __init__(self, url: str, db: str, username: str, password: str):
        self.url = url
        self.db = db
        self.username = username
        self.password = password

        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self.uid = common.authenticate(db, username, password, {})
        self._local = threading.local()

    @property
    def models(self):
        """xmlrpc.client's ServerProxy is not thread-safe, and the engine calls Odoo from the
        request loop, a thread pool and the background sweeper, so each thread gets its own."""
        proxy = getattr(self._local, "proxy", None)
        if proxy is None:
            proxy = self._local.proxy = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        return proxy

    def search_read(self, model: str, domain: list, fields: list, **options) -> list[dict]:
        """`options` (limit, order) are passed through only when given, so the plain
        three-argument call is unchanged."""
        return self.models.execute_kw(
            self.db,
            self.uid,
            self.password,
            model,
            "search_read",
            [domain],
            {"fields": fields, **options},
        )

    def create(self, model: str, values: dict) -> int:
        return self.models.execute_kw(
            self.db,
            self.uid,
            self.password,
            model,
            "create",
            [values],
        )

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        return self.models.execute_kw(self.db, self.uid, self.password, model, "write", [ids, values])

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        """Call any public model method, e.g. crm.lead.activity_schedule."""
        return self.models.execute_kw(self.db, self.uid, self.password, model, method, args, kwargs or {})
