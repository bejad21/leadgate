"""Apply the SQL files in migrations/ to the Supabase project, in order, once each.

    python scripts/apply_migrations.py            # apply what is missing
    python scripts/apply_migrations.py --status   # list applied and pending files, change nothing

Needs SUPABASE_URL and SUPABASE_DB_PASSWORD (Project Settings > Database) in .env. The service_role key
cannot run DDL, because PostgREST has no "run SQL" endpoint, so this connects to Postgres directly.

Every applied file is recorded in public.schema_migrations with its checksum. Re-running applies only
new files and changes nothing else. A file that was edited after it was applied stops the run: put the
change in a new numbered file instead. Each file runs in one transaction, so a failure leaves the
database as it was before that file. The files are also idempotent (`if not exists`), so applying them
to a project that already has the tables is safe.
"""
import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from engine import supabase_rest  # noqa: E402

MIGRATIONS_DIR = ROOT / "migrations"

CREATE_TRACKING_TABLE = """
create table if not exists public.schema_migrations (
  filename text primary key,
  checksum text not null,
  applied_at timestamptz not null default now()
);
alter table public.schema_migrations enable row level security;
revoke all on public.schema_migrations from anon, authenticated;
"""


@dataclass(frozen=True)
class Migration:
    filename: str
    checksum: str
    sql: str


class MigrationError(RuntimeError):
    pass


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        migrations.append(Migration(path.name, hashlib.sha256(sql.encode("utf-8")).hexdigest(), sql))
    return migrations


def plan(migrations: list[Migration], applied: dict[str, str]) -> list[Migration]:
    """The migrations still to apply, in order. Raises MigrationError if an applied one was edited
    or has gone missing from the folder."""
    known = {m.filename for m in migrations}
    for filename in sorted(applied):
        if filename not in known:
            raise MigrationError(f"{filename} was applied but is not in migrations/ any more")
    pending = []
    for migration in migrations:
        recorded = applied.get(migration.filename)
        if recorded is None:
            pending.append(migration)
        elif recorded != migration.checksum:
            raise MigrationError(
                f"{migration.filename} was edited after it was applied. Revert it and put the change in a new numbered file."
            )
    return pending


def connect():
    """A psycopg2 connection to the project's Postgres. Needs SUPABASE_URL and SUPABASE_DB_PASSWORD."""
    import psycopg2

    ref = supabase_rest.project_ref()
    password = os.environ.get("SUPABASE_DB_PASSWORD")
    if not ref or not password:
        raise MigrationError("set SUPABASE_URL and SUPABASE_DB_PASSWORD in .env")
    return psycopg2.connect(
        host=f"db.{ref}.supabase.co", port=5432, dbname="postgres", user="postgres", password=password, connect_timeout=10
    )


def applied_migrations(cur, *, create: bool = True) -> dict[str, str]:
    if create:
        cur.execute(CREATE_TRACKING_TABLE)
    else:
        cur.execute("select to_regclass('public.schema_migrations') is not null")
        if not cur.fetchone()[0]:
            return {}
    cur.execute("select filename, checksum from public.schema_migrations")
    return {filename: checksum for filename, checksum in cur.fetchall()}


def apply_pending(conn, migrations: list[Migration], out=print) -> list[str]:
    """Apply every pending migration, each in its own transaction. Returns the filenames applied."""
    with conn.cursor() as cur:
        applied = applied_migrations(cur)
    conn.commit()
    done = []
    for migration in plan(migrations, applied):
        try:
            with conn.cursor() as cur:
                cur.execute(migration.sql)
                cur.execute(
                    "insert into public.schema_migrations (filename, checksum) values (%s, %s)",
                    (migration.filename, migration.checksum),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        out(f"applied {migration.filename}")
        done.append(migration.filename)
    if not done:
        out("nothing to apply")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--status", action="store_true", help="list applied and pending files and change nothing")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    migrations = load_migrations()
    try:
        conn = connect()
    except MigrationError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # The message of a connection error can include the host; the type is enough to act on.
        print(f"BLOCKED: could not connect to Postgres ({type(exc).__name__}). Check SUPABASE_URL and SUPABASE_DB_PASSWORD.", file=sys.stderr)
        return 2
    try:
        if args.status:
            with conn.cursor() as cur:
                applied = applied_migrations(cur, create=False)
            conn.commit()
            pending = {m.filename for m in plan(migrations, applied)}
            for migration in migrations:
                print(f"{'pending' if migration.filename in pending else 'applied':8} {migration.filename}")
            return 0
        apply_pending(conn, migrations)
        return 0
    except MigrationError as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
