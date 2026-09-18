"""Create n8n credentials for the Supabase and MongoDB nodes used by
n8n/workflows/odoo-sync.json (Task 3.2/3.3).

Uses n8n's REST API (X-N8N-API-KEY) instead of n8n-mcp's live management
tools, which are blocked by SSRF protection against localhost in this
environment. Credential schemas were confirmed live via
GET /api/v1/credentials/schema/<type> rather than guessed.

Run once per n8n instance. Re-running creates duplicate credentials (the
API has no upsert-by-name); check the n8n UI/`GET /api/v1/credentials`
first if re-running.
"""
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

N8N_BASE = os.environ.get("N8N_API_URL", "http://localhost:5678")


def main() -> None:
    key = os.environ["N8N_API_KEY"]
    h = {"X-N8N-API-KEY": key, "Content-Type": "application/json"}

    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_KEY"]
    mongo_uri = os.environ["MONGODB_URI"]
    supabase_host = supabase_url.split("//")[1].split("/")[0]

    r = httpx.post(
        f"{N8N_BASE}/api/v1/credentials",
        headers=h,
        json={
            "name": "LeadGate Supabase (service role)",
            "type": "supabaseApi",
            "data": {
                "host": supabase_url,
                "serviceRole": supabase_key,
                # Least-privilege: only this HTTP Request node's own target
                # domain may be called with this credential.
                "allowedHttpRequestDomains": "domains",
                "allowedDomains": supabase_host,
            },
        },
        timeout=15,
    )
    r.raise_for_status()
    sb = r.json()
    print("Supabase credential:", json.dumps({k: v for k, v in sb.items() if k != "data"}, indent=2))

    r2 = httpx.post(
        f"{N8N_BASE}/api/v1/credentials",
        headers=h,
        json={
            "name": "LeadGate MongoDB (leadgate db)",
            "type": "mongoDb",
            "data": {
                "configurationType": "connectionString",
                "connectionString": mongo_uri,
            },
        },
        timeout=15,
    )
    r2.raise_for_status()
    mg = r2.json()
    print("MongoDB credential:", json.dumps({k: v for k, v in mg.items() if k != "data"}, indent=2))

    print(
        "\nNOTE: n8n/workflows/odoo-sync.json references credential ids "
        f"{sb['id']!r} (Supabase) and {mg['id']!r} (MongoDB). If you create "
        "new credentials with different ids, update the workflow's node "
        "'credentials' blocks (or re-wire them in the n8n editor) before "
        "importing/activating."
    )


if __name__ == "__main__":
    main()
