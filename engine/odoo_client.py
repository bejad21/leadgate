import math
import os
import threading
import xmlrpc.client
from typing import cast

# Seconds a call to Odoo may wait on the socket before it fails. Without a limit one stuck Odoo
# holds the calling thread, and with it the customer's turn, for as long as the connection lives.
# ODOO_TIMEOUT_SECONDS overrides it; it is read when a client is built, after .env has been loaded.
DEFAULT_TIMEOUT_SECONDS = 30.0


def _check_timeout(value, source: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{source} must be a number of seconds greater than 0, got {value!r}")
    return seconds


def resolve_timeout(timeout: float | None = None) -> float:
    if timeout is not None:
        return _check_timeout(timeout, "timeout")
    raw = (os.environ.get("ODOO_TIMEOUT_SECONDS") or "").strip()
    return _check_timeout(raw, "ODOO_TIMEOUT_SECONDS") if raw else DEFAULT_TIMEOUT_SECONDS


class OdooLoginError(RuntimeError):
    """Odoo refused the configured user. The password never appears in the message."""


class _TimeoutMixin(xmlrpc.client.Transport):
    def __init__(self, timeout: float, **kwargs):
        super().__init__(**kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


class _Transport(_TimeoutMixin, xmlrpc.client.Transport):
    pass


class _SafeTransport(_TimeoutMixin, xmlrpc.client.SafeTransport):
    pass


def make_transport(url: str, timeout: float):
    """An XML-RPC transport for `url` whose sockets give up after `timeout` seconds."""
    return (_SafeTransport if url.lower().startswith("https") else _Transport)(timeout)


class OdooClient:
    """Thin wrapper around Odoo's XML-RPC API for common/object endpoints."""

    def __init__(self, url: str, db: str, username: str, password: str, timeout: float | None = None):
        self.url = url
        self.timeout = resolve_timeout(timeout)
        self.db = db
        self.username = username
        self.password = password

        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", transport=make_transport(url, self.timeout))
        self.uid = common.authenticate(db, username, password, {})
        if not self.uid:
            # authenticate() answers False for bad credentials, which would otherwise surface later as an opaque fault.
            raise OdooLoginError(f"Odoo rejected the login for user {username} on db {db}")
        self._local = threading.local()

    @property
    def models(self):
        """xmlrpc.client's ServerProxy is not thread-safe, and the engine calls Odoo from the
        request loop and a thread pool, so each thread gets its own."""
        proxy = getattr(self._local, "proxy", None)
        if proxy is None:
            proxy = self._local.proxy = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/object", transport=make_transport(self.url, self.timeout)
            )
        return proxy

    def search_read(self, model: str, domain: list, fields: list, **options) -> list[dict]:
        """`options` (limit, order) are passed through only when given, so the plain
        three-argument call is unchanged."""
        return cast(
            list[dict],
            self.models.execute_kw(
                self.db,
                self.uid,
                self.password,
                model,
                "search_read",
                [domain],
                {"fields": fields, **options},
            ),
        )

    def create(self, model: str, values: dict) -> int:
        return cast(
            int,
            self.models.execute_kw(
                self.db,
                self.uid,
                self.password,
                model,
                "create",
                [values],
            ),
        )

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        return cast(bool, self.models.execute_kw(self.db, self.uid, self.password, model, "write", [ids, values]))

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        """Call any public model method, e.g. crm.lead.activity_schedule."""
        return self.models.execute_kw(self.db, self.uid, self.password, model, method, args, kwargs or {})
