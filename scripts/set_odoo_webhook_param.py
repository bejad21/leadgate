"""Point Odoo at the engine's /webhook/odoo-status: set the two system parameters the addon reads.

    python scripts/set_odoo_webhook_param.py

System parameters need an administrator: ODOO_ADMIN_USER and ODOO_ADMIN_PASSWORD are used when set.
Needs ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD and ODOO_WEBHOOK_SECRET (at least 16 characters; it
must be the same value the engine runs with) in .env. The address is the engine's name on the Compose
network, not `localhost`: Odoo's outbound POST is made from inside the `odoo` container, which cannot
reach the host's published loopback port. Override it with ODOO_STATUS_WEBHOOK_URL if the engine runs
elsewhere. Also removes the parameter an older version of the project used.
"""
import os
import sys
import xmlrpc.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from engine import odoo_status  # noqa: E402

DEFAULT_WEBHOOK_URL = "http://engine:8000/webhook/odoo-status"
URL_PARAM = "leadgate.status_webhook_url"
SECRET_PARAM = "leadgate.status_webhook_secret"
OLD_PARAM = "leadgate.sync_webhook_url"


def main() -> int:
    load_dotenv(ROOT / ".env")
    problem = odoo_status.secret_problem(os.environ.get("ODOO_WEBHOOK_SECRET"))
    if problem:
        print(f"BLOCKED: {problem}", file=sys.stderr)
        return 2
    secret = os.environ["ODOO_WEBHOOK_SECRET"]
    webhook_url = os.environ.get("ODOO_STATUS_WEBHOOK_URL") or DEFAULT_WEBHOOK_URL

    url = os.environ.get("ODOO_URL", "http://localhost:8069")
    db = os.environ["ODOO_DB"]
    username = os.environ.get("ODOO_ADMIN_USER") or os.environ["ODOO_USER"]
    password = os.environ.get("ODOO_ADMIN_PASSWORD") or os.environ["ODOO_PASSWORD"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, password, {})
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def call(model, method, *args, **kwargs):
        return models.execute_kw(db, uid, password, model, method, list(args), kwargs)

    def set_param(key, value):
        ids = call("ir.config_parameter", "search", [["key", "=", key]])
        if ids:
            call("ir.config_parameter", "write", ids, {"value": value})
        else:
            call("ir.config_parameter", "create", {"key": key, "value": value})

    set_param(URL_PARAM, webhook_url)
    set_param(SECRET_PARAM, secret)
    old = call("ir.config_parameter", "search", [["key", "=", OLD_PARAM]])
    if old:
        call("ir.config_parameter", "unlink", old)
        print(f"removed the old parameter {OLD_PARAM}")
    print(f"{URL_PARAM} = {call('ir.config_parameter', 'get_param', URL_PARAM)}")
    print(f"{SECRET_PARAM} is set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
