from src.alpaca_research.config import load_settings


def test_default_is_paper_only(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    settings = load_settings(False)
    assert settings.paper is True
    assert settings.trading_base_url == "https://paper-api.alpaca.markets"


def test_live_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    try:
        load_settings(False)
    except RuntimeError as exc:
        assert "Live Alpaca mode is disabled" in str(exc)
    else:
        raise AssertionError("live mode must be rejected")
