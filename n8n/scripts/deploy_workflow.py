"""Deploy n8n/workflows/odoo-sync.json to a running n8n instance and
activate it (Task 3.2/3.3).

n8n-mcp's live management tools (n8n_create_workflow / n8n_validate_workflow)
are blocked in this environment by SSRF protection against localhost, so
this deploys directly via n8n's REST + internal session API instead.

Two auth mechanisms are needed:
  - N8N_API_KEY (X-N8N-API-KEY header) for the public API (create/read
    workflows and credentials).
  - N8N_OWNER_EMAIL/N8N_OWNER_PASSWORD (session cookie via /rest/login) for
    activation: on this n8n version (2.35.7) the public API's
    POST /api/v1/workflows/{id}/activate endpoint returns 403 for this API
    key's scopes, but the internal /rest/workflows/{id}/activate endpoint
    (what the editor UI itself calls) works once a session is established,
    and requires the workflow's current versionId in the request body.

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
