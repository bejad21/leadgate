"""Create the owner's dashboard login in Supabase Auth.

Leads and conversations are private, so the dashboard asks for a sign-in before it
shows them, and the database only lets accounts marked role = 'staff' read them.
This creates that account (already confirmed, so no email is sent) from
DASHBOARD_LOGIN_EMAIL and DASHBOARD_LOGIN_PASSWORD in .env, with that role in its
app_metadata (which only the server can set). If the account already exists its
password and role are updated, so running it twice is safe.

Also worth doing in the Supabase dashboard: Authentication > Sign In / Providers >
turn off "Allow new users to sign up". The staff-only policy already protects the
data while sign-ups are open, but there is no reason to leave them open.

Usage (repo root, virtualenv active):
    python n8n/scripts/create_dashboard_user.py
"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def main() -> int:
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/auth/v1/admin/users"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    email = os.environ.get("DASHBOARD_LOGIN_EMAIL")
    password = os.environ.get("DASHBOARD_LOGIN_PASSWORD")
    if not email or not password:
        print("Set DASHBOARD_LOGIN_EMAIL and DASHBOARD_LOGIN_PASSWORD in .env first.")
        return 1
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    response = httpx.post(url, headers=headers, json={"email": email, "password": password, "email_confirm": True, "app_metadata": {"role": "staff"}}, timeout=30)
    if response.status_code in (200, 201):
        print(f"Created dashboard login for {email}.")
        return 0

    # Already registered: find the account and set the password.
    listing = httpx.get(url, headers=headers, params={"per_page": 200}, timeout=30)
    listing.raise_for_status()
    for user in listing.json().get("users", []):
        if user.get("email", "").lower() == email.lower():
            update = httpx.put(f"{url}/{user['id']}", headers=headers, json={"password": password, "email_confirm": True, "app_metadata": {"role": "staff"}}, timeout=30)
            update.raise_for_status()
            print(f"Dashboard login for {email} already existed; password updated.")
            return 0
    print(f"Could not create the login: HTTP {response.status_code} {response.text[:200]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
