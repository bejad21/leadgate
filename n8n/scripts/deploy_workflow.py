"""Deploy n8n/workflows/odoo-sync.json to a running n8n instance and activate it.

Uses n8n's REST API and its internal session API directly.

Two kinds of auth are needed:
  - N8N_API_KEY (X-N8N-API-KEY header) for the public API (create and read workflows and
    credentials).
  - N8N_OWNER_EMAIL / N8N_OWNER_PASSWORD (a session cookie from /rest/login) for activation. On
    the n8n version tested (2.35.7) the public POST /api/v1/workflows/{id}/activate returns 403 for this API key's
    scopes, but the internal /rest/workflows/{id}/activate endpoint (the one the editor calls)
    works with a session and needs the workflow's current versionId in the body.

Run: .venv/Scripts/python.exe n8n/scripts/deploy_workflow.py
"""
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

N8N_BASE = os.environ.get("N8N_API_URL", "http://localhost:5678")
WORKFLOW_PATH = os.path.join(os.path.dirname(__file__), "..", "workflows", "odoo-sync.json")


def main() -> None:
    key = os.environ["N8N_API_KEY"]
    h = {"X-N8N-API-KEY": key, "Content-Type": "application/json"}

    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        wf = json.load(f)
    wf.pop("active", None)  # active is read-only on create/update via the public API

    r = httpx.post(f"{N8N_BASE}/api/v1/workflows", headers=h, json=wf, timeout=20)
    r.raise_for_status()
    created = r.json()
    wf_id = created["id"]
    print("Created workflow", wf_id)

    email = os.environ["N8N_OWNER_EMAIL"]
    password = os.environ["N8N_OWNER_PASSWORD"]
    client = httpx.Client(base_url=N8N_BASE, timeout=20)
    client.post("/rest/login", json={"emailOrLdapLoginId": email, "password": password}).raise_for_status()

    current = client.get(f"/rest/workflows/{wf_id}").json()["data"]
    activated = client.post(
        f"/rest/workflows/{wf_id}/activate", json={"versionId": current["versionId"]}
    )
    activated.raise_for_status()
    print("Activated:", activated.json()["data"]["active"])

    r2 = httpx.get(f"{N8N_BASE}/api/v1/workflows/{wf_id}", headers=h, timeout=20)
    print("Connections:", json.dumps(r2.json()["connections"], indent=2))


if __name__ == "__main__":
    main()
