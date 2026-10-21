from kalshi_moonshot import commodities as cm
from kalshi_moonshot import config


def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("COMMODITY_15M_LIVE", "true")
    monkeypatch.setenv("LIVE_LANES", "commodity")
    return config.load()


def test_currencies_are_live_and_indices_and_brent_stay_paper_only(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    for series in ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M"):
        assert cm.instrument_for(series).live and s.is_live("commodity", series), series
    for series in ("KXINX15M", "KXNDQ15M", "KXBRENT15M"):
        inst = cm.instrument_for(series)
        assert inst is not None and not inst.live, series
        assert not s.is_live("commodity", series), series


def test_existing_markets_stay_live(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    for series in ("KXBTC15M", "KXGOLD15M", "KXWTI15M", "KXBRENTD", "KXGOLDH"):
        assert s.is_live("commodity", series), series
    assert s.is_live("commodity")  # no series named: the lane itself is live
