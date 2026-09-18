"""Create the Supabase `catalog_items` table (Task 3.2).

The service_role REST API key alone (SUPABASE_URL + SUPABASE_SERVICE_KEY) is
NOT enough to run DDL -- PostgREST intentionally has no "run arbitrary SQL"
endpoint. This script needs ONE of the following additional secrets added to
.env before it can run:

  - SUPABASE_DB_PASSWORD: the project's direct Postgres password (Supabase
    dashboard -> Project Settings -> Database -> Connection string). The
    script then connects directly via psycopg2 to
    db.<project-ref>.supabase.co:5432 and executes the DDL.

  - SUPABASE_ACCESS_TOKEN: a personal access token (starts with `sbp_`,
    created in the Supabase dashboard under Account -> Access Tokens). The
    script then calls the Management API's
    POST /v1/projects/{ref}/database/query endpoint.

Without either, this script prints a clear explanation and exits non-zero
instead of guessing at credentials.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

SQL_PATH = os.path.join(os.path.dirname(__file__), "setup_supabase_table.sql")


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
    print(f"Created/verified public.catalog_items via direct Postgres connection to {host}.")
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
