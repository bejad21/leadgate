"""scripts/set_odoo_webhook_param.py refuses to run without a usable secret, and says why."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("set_odoo_webhook_param", ROOT / "scripts" / "set_odoo_webhook_param.py")
assert spec is not None and spec.loader is not None
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


def test_without_a_secret_it_stops_before_touching_odoo(monkeypatch, capsys):
    monkeypatch.delenv("ODOO_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(script.xmlrpc.client, "ServerProxy", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not connect")))
    assert script.main() == 2
    assert "ODOO_WEBHOOK_SECRET" in capsys.readouterr().err


def test_a_short_secret_is_refused_and_not_echoed(monkeypatch, capsys):
    monkeypatch.setenv("ODOO_WEBHOOK_SECRET", "short-one")
    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: None)
    assert script.main() == 2
    captured = capsys.readouterr()
    assert "at least" in captured.err and "short-one" not in captured.err + captured.out


def test_the_default_address_is_the_engine_on_the_compose_network():
    assert script.DEFAULT_WEBHOOK_URL == "http://engine:8000/webhook/odoo-status"
