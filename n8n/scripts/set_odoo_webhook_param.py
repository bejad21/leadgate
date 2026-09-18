"""Point Odoo's leadgate.sync_webhook_url System Parameter at the deployed
n8n workflow's webhook (Task 3.2/3.3).

Uses the container-network hostname (`n8n`, the docker-compose service
name), not `localhost`, because Odoo's outbound webhook POST is made from
inside the `leadgate-odoo` container, which cannot resolve the host's
localhost/loopback-published port mapping.
"""
import os
import xmlrpc.client

from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = "http://n8n:5678/webhook/odoo-sync"


def main() -> None:
    url = os.environ.get("ODOO_URL", "http://localhost:8069")
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USER"]
    password = os.environ["ODOO_PASSWORD"]

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, password, {})
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    def call(model, method, *args, **kwargs):
        return models.execute_kw(db, uid, password, model, method, list(args), kwargs)

    current = call("ir.config_parameter", "get_param", "leadgate.sync_webhook_url")
    print("current value:", current)

    param_ids = call("ir.config_parameter", "search", [["key", "=", "leadgate.sync_webhook_url"]])
    if param_ids:
        call("ir.config_parameter", "write", param_ids, {"value": WEBHOOK_URL})
    else:
        call("ir.config_parameter", "create", {"key": "leadgate.sync_webhook_url", "value": WEBHOOK_URL})

    print("new value:", call("ir.config_parameter", "get_param", "leadgate.sync_webhook_url"))


if __name__ == "__main__":
    main()
