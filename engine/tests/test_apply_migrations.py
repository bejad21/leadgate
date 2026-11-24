"""scripts/apply_migrations.py: which files run, in what order, and what stops it. No database is used:
a recording connection stands in for psycopg2."""
import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("apply_migrations", ROOT / "scripts" / "apply_migrations.py")
assert spec is not None and spec.loader is not None
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)


def migration(name, sql="select 1;"):
    return am.Migration(name, hashlib.sha256(sql.encode()).hexdigest(), sql)


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if self.conn.fail_on and self.conn.fail_on in text:
            raise RuntimeError("syntax error")
        self.conn.executed.append((text, params))
        if text.startswith("select filename, checksum"):
            self.rows = list(self.conn.applied.items())
        elif text.startswith("insert into public.schema_migrations"):
            self.conn.pending_inserts.append(params)
        elif "to_regclass" in text:
            self.rows = [(self.conn.tracking_exists,)]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class Conn:
    def __init__(self, applied=None, fail_on=None, tracking_exists=True):
        self.applied = dict(applied or {})
        self.fail_on = fail_on
        self.tracking_exists = tracking_exists
        self.executed = []
        self.pending_inserts = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1
        for filename, checksum in self.pending_inserts:
            self.applied[filename] = checksum
        self.pending_inserts = []

    def rollback(self):
        self.rollbacks += 1
        self.pending_inserts = []

    def close(self):
        pass


# The committed migrations

def test_the_committed_migrations_are_numbered_in_order_with_no_gaps_or_repeats():
    names = [m.filename for m in am.load_migrations()]
    numbers = [int(n[:4]) for n in names if re.fullmatch(r"\d{4}_\w+\.sql", n)]
    assert numbers == list(range(1, len(names) + 1))


def test_every_committed_migration_is_safe_to_run_twice():
    for m in am.load_migrations():
        for statement in re.findall(r"^\s*(create table|create index|alter table [\w.]+ add column)\b(?! if not exists)", m.sql, flags=re.I | re.M):
            raise AssertionError(f"{m.filename}: '{statement}' without if not exists")
        assert not re.search(r"^\s*drop (table|column)\b", m.sql, flags=re.I | re.M), f"{m.filename} drops data"


def test_every_new_table_has_row_level_security_and_no_policy_for_the_browser():
    sql = (ROOT / "migrations" / "0003_engine_state.sql").read_text(encoding="utf-8")
    tables = re.findall(r"create table if not exists public\.(\w+)", sql)
    assert set(tables) == {"chat_turns", "lead_chats", "alert_messages", "processed_updates", "rate_limits", "lead_flags"}
    for table in tables:
        assert f"alter table public.{table} enable row level security" in sql
    assert "create policy" not in sql.lower()
    assert "revoke all on function public.rate_limit_hit" in sql and "grant execute on function public.rate_limit_hit" in sql


def test_the_tables_the_dashboard_reads_are_only_added_to_never_changed():
    for m in am.load_migrations():
        if m.filename.startswith("0003"):
            for shared in ("catalog_items", "public.leads", "conversation_turns"):
                assert not re.search(rf"(alter|drop)\s+table\s+(public\.)?{shared}", m.sql, flags=re.I)


# Planning

def test_nothing_applied_means_everything_is_pending_in_order():
    files = [migration("0001_a.sql"), migration("0002_b.sql")]
    assert [m.filename for m in am.plan(files, {})] == ["0001_a.sql", "0002_b.sql"]


def test_applied_files_are_skipped():
    files = [migration("0001_a.sql"), migration("0002_b.sql")]
    assert [m.filename for m in am.plan(files, {"0001_a.sql": files[0].checksum})] == ["0002_b.sql"]


def test_an_edited_applied_file_stops_the_run():
    files = [migration("0001_a.sql", "select 2;")]
    with pytest.raises(am.MigrationError, match="edited after it was applied"):
        am.plan(files, {"0001_a.sql": "someotherchecksum"})


def test_an_applied_file_that_went_missing_stops_the_run():
    with pytest.raises(am.MigrationError, match="not in migrations/"):
        am.plan([migration("0002_b.sql")], {"0001_a.sql": "x"})


# Applying

def test_each_pending_file_runs_and_is_recorded_in_its_own_transaction():
    files = [migration("0001_a.sql", "create table a();"), migration("0002_b.sql", "create table b();")]
    conn, out = Conn(), []
    done = am.apply_pending(conn, files, out=out.append)
    assert done == ["0001_a.sql", "0002_b.sql"] and out == ["applied 0001_a.sql", "applied 0002_b.sql"]
    assert conn.applied == {m.filename: m.checksum for m in files}
    assert [sql for sql, _ in conn.executed if sql in ("create table a();", "create table b();")] == ["create table a();", "create table b();"]
    assert conn.commits == 3 and conn.rollbacks == 0  # one for the tracking table, one per file


def test_running_again_applies_nothing():
    files = [migration("0001_a.sql")]
    conn, out = Conn(), []
    am.apply_pending(conn, files, out=out.append)
    executed_before = len(conn.executed)
    assert am.apply_pending(conn, files, out=out.append) == []
    assert out[-1] == "nothing to apply"
    assert not any(sql == "select 1;" for sql, _ in conn.executed[executed_before:])


def test_a_failing_file_is_rolled_back_not_recorded_and_stops_the_run():
    files = [migration("0001_a.sql", "create table a();"), migration("0002_b.sql", "boom;"), migration("0003_c.sql", "create table c();")]
    conn = Conn(fail_on="boom;")
    with pytest.raises(RuntimeError):
        am.apply_pending(conn, files, out=lambda _: None)
    assert list(conn.applied) == ["0001_a.sql"] and conn.rollbacks == 1
    assert not any("create table c" in sql for sql, _ in conn.executed)


def test_the_tracking_table_is_private():
    assert "enable row level security" in am.CREATE_TRACKING_TABLE and "revoke all" in am.CREATE_TRACKING_TABLE


# The command

def test_status_changes_nothing_and_lists_each_file(monkeypatch, capsys):
    files = [migration("0001_a.sql"), migration("0002_b.sql")]
    conn = Conn(applied={"0001_a.sql": files[0].checksum})
    monkeypatch.setattr(am, "load_migrations", lambda: files)
    monkeypatch.setattr(am, "connect", lambda: conn)
    assert am.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert "applied  0001_a.sql" in out and "pending  0002_b.sql" in out
    assert not any(sql.startswith("create table") or sql.startswith("insert") for sql, _ in conn.executed)


def test_status_before_the_tracking_table_exists_lists_everything_as_pending(monkeypatch, capsys):
    files = [migration("0001_a.sql")]
    conn = Conn(tracking_exists=False)
    monkeypatch.setattr(am, "load_migrations", lambda: files)
    monkeypatch.setattr(am, "connect", lambda: conn)
    assert am.main(["--status"]) == 0
    assert "pending  0001_a.sql" in capsys.readouterr().out


def test_without_credentials_it_says_what_to_set_and_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_PASSWORD", raising=False)
    monkeypatch.setattr(am, "load_dotenv", lambda *a, **k: None)
    pytest.importorskip("psycopg2")
    assert am.main([]) == 2
    assert "SUPABASE_DB_PASSWORD" in capsys.readouterr().err


def test_a_connection_failure_exits_2_without_printing_the_host_or_password(monkeypatch, capsys):
    def fail():
        raise OSError("could not translate host name db.secretref.supabase.co with password hunter2")

    monkeypatch.setattr(am, "connect", fail)
    monkeypatch.setattr(am, "load_dotenv", lambda *a, **k: None)
    assert am.main([]) == 2
    err = capsys.readouterr().err
    assert "OSError" in err and "hunter2" not in err and "secretref" not in err


def test_an_edited_migration_makes_the_command_exit_1(monkeypatch, capsys):
    files = [migration("0001_a.sql", "select 2;")]
    conn = Conn(applied={"0001_a.sql": "old"})
    monkeypatch.setattr(am, "load_migrations", lambda: files)
    monkeypatch.setattr(am, "connect", lambda: conn)
    monkeypatch.setattr(am, "load_dotenv", lambda *a, **k: None)
    assert am.main([]) == 1
    assert "edited after it was applied" in capsys.readouterr().err
