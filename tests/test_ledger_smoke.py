"""The smoke check that is run against the real database after deployment also passes against the memory store."""

from app.ledger import smoke
from app.ledger import store as ledger_store

from .test_authorization_api import make_settings


def test_the_smoke_check_passes_and_cleans_up_after_itself(monkeypatch, capsys):
    settings = make_settings("easyauth", ledger_backend="memory")
    monkeypatch.setattr(smoke, "get_settings", lambda: settings)

    assert smoke.main() == 0

    output = capsys.readouterr().out
    assert "all checks passed" in output and "FAIL" not in output
    assert ledger_store.store_for(settings).list_projects(owner="x", groups=[], everything=True) == []


def test_the_smoke_check_fails_clearly_when_the_ledger_is_off(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "get_settings", lambda: make_settings("easyauth"))
    assert smoke.main() == 1
    assert "COSMOS_ENDPOINT" in capsys.readouterr().out
