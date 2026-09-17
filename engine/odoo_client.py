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
        self.models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def search_read(self, model: str, domain: list, fields: list) -> list[dict]:
        return self.models.execute_kw(
            self.db,
            self.uid,
            self.password,
            model,
            "search_read",
            [domain],
            {"fields": fields},
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
