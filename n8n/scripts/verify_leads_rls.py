"""Check who can read the private tables, from the point of view of each kind of visitor.

Leads and conversations hold customers' names, contact details and messages. This
plants a probe row and confirms that:
  - an anonymous visitor sees nothing,
  - a signed-in stranger (a plain account that anyone can register, if sign-ups are
    open) sees nothing,
  - the owner (the account made by create_dashboard_user.py) sees the row,
  - even the owner cannot write with the browser key.
The probe row and the stranger account are removed afterwards.

Usage (repo root, virtualenv active):
    python n8n/scripts/verify_leads_rls.py
"""
import os
import secrets
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

URL = os.environ["SUPABASE_URL"].rstrip("/")
ANON = os.environ["SUPABASE_ANON_KEY"]
SERVICE = os.environ["SUPABASE_SERVICE_KEY"]
svc = {"apikey": SERVICE, "Authorization": f"Bearer {SERVICE}", "Content-Type": "application/json", "Prefer": "return=minimal"}
results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def sign_in(email: str, password: str) -> str:
    r = httpx.post(f"{URL}/auth/v1/token?grant_type=password", headers={"apikey": ANON, "Content-Type": "application/json"}, json={"email": email, "password": password}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def read(token: str | None, table: str) -> list:
    headers = {"apikey": ANON}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(f"{URL}/rest/v1/{table}?select=id&chat_ref=eq.rls-probe", headers=headers, timeout=30)
    return r.json() if r.status_code == 200 else []


def main() -> int:
    stranger_id = None
    try:
        httpx.post(f"{URL}/rest/v1/leads", headers=svc, json={"odoo_lead_id": -777, "domain_type": "cars", "item_name": "rls probe", "chat_ref": "rls-probe"}, timeout=30)
        httpx.post(f"{URL}/rest/v1/conversation_turns", headers=svc, json={"chat_ref": "rls-probe", "domain_type": "cars", "user_message": "probe", "reply": "probe"}, timeout=30)

        stranger_email = f"stranger.{secrets.token_hex(4)}@leadgate-check.dev"
        stranger_password = secrets.token_urlsafe(14)
        created = httpx.post(f"{URL}/auth/v1/admin/users", headers=svc, json={"email": stranger_email, "password": stranger_password, "email_confirm": True}, timeout=30)
        created.raise_for_status()
        stranger_id = created.json()["id"]
        stranger = sign_in(stranger_email, stranger_password)
        owner = sign_in(os.environ["DASHBOARD_LOGIN_EMAIL"], os.environ["DASHBOARD_LOGIN_PASSWORD"])

        for table in ("leads", "conversation_turns"):
            check(f"anonymous visitor cannot read {table}", read(None, table) == [])
            check(f"a signed-in stranger cannot read {table}", read(stranger, table) == [], f"saw {len(read(stranger, table))} row(s)")
            check(f"the owner can read {table}", len(read(owner, table)) == 1)

        def catalog_rows(token: str) -> int:
            r = httpx.get(f"{URL}/rest/v1/catalog_items?select=id&limit=1", headers={"apikey": ANON, "Authorization": f"Bearer {token}"}, timeout=30)
            return len(r.json()) if r.status_code == 200 else 0

        check("a signed-in user still sees the public catalog", catalog_rows(owner) > 0 and catalog_rows(stranger) > 0)
        write = httpx.post(f"{URL}/rest/v1/leads", headers={"apikey": ANON, "Authorization": f"Bearer {owner}", "Content-Type": "application/json"}, json={"odoo_lead_id": -778, "domain_type": "cars", "item_name": "x", "chat_ref": "rls-probe"}, timeout=30)
        check("even the owner cannot write with the browser key", write.status_code in (401, 403), str(write.status_code))
    finally:
        for table in ("leads", "conversation_turns"):
            httpx.delete(f"{URL}/rest/v1/{table}?chat_ref=eq.rls-probe", headers=svc, timeout=30)
        if stranger_id:
            httpx.delete(f"{URL}/auth/v1/admin/users/{stranger_id}", headers=svc, timeout=30)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
