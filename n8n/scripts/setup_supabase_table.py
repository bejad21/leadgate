"""Create the Supabase `catalog_items` table, or apply another SQL file passed as an argument.

The service_role REST key alone cannot run DDL, because PostgREST has no "run arbitrary SQL"
endpoint. Add ONE of these to .env:

  - SUPABASE_DB_PASSWORD: the project's Postgres password (Project Settings > Database >
    Connection string). The script connects with psycopg2 to db.<project-ref>.supabase.co:5432
    and runs the DDL.
  - SUPABASE_ACCESS_TOKEN: a personal access token (starts with `sbp_`, from Account > Access
    Tokens). The script calls the Management API's POST /v1/projects/{ref}/database/query.

With neither, it prints an explanation and exits non-zero.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Defaults to the catalog table; pass another file name to apply a different script,
# e.g. `python n8n/scripts/setup_supabase_table.py setup_supabase_leads.sql`.
SQL_PATH = os.path.join(
    os.path.dirname(__file__), sys.argv[1] if len(sys.argv) > 1 else "setup_supabase_table.sql"
)


def read_sql() -> str:
    with open(SQL_PATH, "r", encoding="utf-8") as f:
        return f.read()


def project_ref(supabase_url: str) -> str:
    return supabase_url.split("//")[1].split(".")[0]


def try_direct_postgres(supabase_url: str) -> bool:
    db_password = os.environ.get("SUPABASE_DB_PASSWORD")
    if not db_password:
        return False
    import psycopg2

    host = f"db.{project_ref(supabase_url)}.supabase.co"
    conn = psycopg2.connect(
        host=host, port=5432, dbname="postgres", user="postgres",
        password=db_password, connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(read_sql())
        conn.commit()
    finally:
        conn.close()
    print(f"Applied {os.path.basename(SQL_PATH)} via direct Postgres connection to {host}.")
    return True


def try_management_api(supabase_url: str) -> bool:
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        return False
    import httpx

    ref = project_ref(supabase_url)
    r = httpx.post(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": read_sql()},
        timeout=30,
    )
    r.raise_for_status()
    print(f"Created/verified public.catalog_items via Supabase Management API (project {ref}).")
    return True


def main() -> int:
    supabase_url = os.environ.get("SUPABASE_URL")
    if not supabase_url:
        print("SUPABASE_URL is not set in .env", file=sys.stderr)
        return 1

    if try_direct_postgres(supabase_url):
        return 0
    if try_management_api(supabase_url):
        return 0

    print(
        "BLOCKED: neither SUPABASE_DB_PASSWORD nor SUPABASE_ACCESS_TOKEN is set in .env.\n"
        "SUPABASE_URL + SUPABASE_SERVICE_KEY (the REST/service-role key) cannot run DDL --\n"
        "PostgREST has no SQL-execution endpoint. Add one of:\n"
        "  SUPABASE_DB_PASSWORD=<Project Settings > Database > Connection string password>\n"
        "  SUPABASE_ACCESS_TOKEN=<Account > Access Tokens PAT, starts with sbp_>\n"
        "to .env, then re-run this script. The DDL itself is in setup_supabase_table.sql.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
