"""Create (or reset) the engine's limited Odoo account, so the engine does not log in as an administrator.

    python scripts/create_odoo_engine_user.py

Needs, in .env or the environment:
- ODOO_URL, ODOO_DB
- ODOO_ADMIN_USER (default admin) and ODOO_ADMIN_PASSWORD: used only by this script
- the account to create: ODOO_USER and ODOO_PASSWORD, the same settings the engine runs with (a password of at
  least 16 characters). ODOO_ENGINE_LOGIN and ODOO_ENGINE_PASSWORD override them; while ODOO_USER is still admin
  the account is leadgate-engine and ODOO_ENGINE_PASSWORD must be given.
- ODOO_ENGINE_EMAIL (optional): the sender address on the account's notes and assignments. Odoo will not assign a
  lead for an account without one; the default is a placeholder under .invalid, which receives nothing.

Set ODOO_SALESPERSON_LOGIN to the person new leads are for, or they are assigned to the engine account. The
leadgate_domain module must be installed or upgraded first; it defines the "LeadGate Engine" group the account gets.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from engine.odoo_client import OdooClient  # noqa: E402

DEFAULT_LOGIN = "leadgate-engine"
MIN_PASSWORD_LENGTH = 16


def ensure_engine_user(odoo, login: str, password: str, email: str | None = None) -> int:
    """The id of the account `login`, created or reset to the engine group only."""
    if login.strip().lower() == "admin":
        raise SystemExit("The engine account must not be admin. Choose another ODOO_ENGINE_LOGIN.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"ODOO_ENGINE_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters.")
    group = odoo.search_read(
        "ir.model.data", [("module", "=", "leadgate_domain"), ("name", "=", "group_engine")], ["res_id"]
    )
    if not group:
        raise SystemExit("The LeadGate Engine group does not exist: upgrade the leadgate_domain module first.")
    values = {
        "password": password,
        "email": email or f"{login}@leadgate.invalid",
        "groups_id": [(6, 0, [group[0]["res_id"]])],
    }
    existing = odoo.search_read("res.users", [("login", "=", login)], ["id"])
    if existing:
        odoo.write("res.users", [existing[0]["id"]], values)
        return existing[0]["id"]
    return odoo.create("res.users", {"name": "LeadGate Engine", "login": login, **values})


def engine_credentials(env) -> tuple[str, str]:
    """(login, password) of the account to create. The administrator's own password is never reused for it."""
    user = (env.get("ODOO_USER") or "").strip()
    login = env.get("ODOO_ENGINE_LOGIN") or (user if user and user.lower() != "admin" else DEFAULT_LOGIN)
    password = env.get("ODOO_ENGINE_PASSWORD") or (env.get("ODOO_PASSWORD", "") if user == login else "")
    return login, password


def main() -> int:
    load_dotenv(ROOT / ".env")
    login, password = engine_credentials(os.environ)
    missing = [name for name in ("ODOO_DB", "ODOO_ADMIN_PASSWORD") if not os.environ.get(name)]
    if not password:
        missing.append("ODOO_ENGINE_PASSWORD (or ODOO_USER and ODOO_PASSWORD of the new account)")
    if missing:
        print("BLOCKED: set " + ", ".join(missing), file=sys.stderr)
        return 2
    admin = OdooClient(
        os.environ.get("ODOO_URL", "http://localhost:8069"), os.environ["ODOO_DB"],
        os.environ.get("ODOO_ADMIN_USER") or "admin", os.environ["ODOO_ADMIN_PASSWORD"],
    )
    user_id = ensure_engine_user(admin, login, password, os.environ.get("ODOO_ENGINE_EMAIL"))
    print(f"engine account {login!r} is ready (user {user_id}). Set ODOO_USER and ODOO_PASSWORD to it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
