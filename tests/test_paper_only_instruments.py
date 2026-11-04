from kalshi_moonshot import commodities as cm
from kalshi_moonshot import config


def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("COMMODITY_15M_LIVE", "true")
    monkeypatch.setenv("LIVE_LANES", "commodity")
    return config.load()


def test_currencies_are_live_and_indices_stay_paper_only(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    for series in ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M"):
        assert cm.instrument_for(series).live and s.is_live("commodity", series), series
    for series in ("KXINX15M", "KXNDQ15M"):
        inst = cm.instrument_for(series)
        assert inst is not None and not inst.live, series
        assert not s.is_live("commodity", series), series


def test_existing_markets_stay_live(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    for series in ("KXBTC15M", "KXGOLD15M", "KXWTI15M", "KXBRENT15M", "KXBRENTD", "KXGOLDH"):
        assert s.is_live("commodity", series), series
    assert s.is_live("commodity")  # no series named: the lane itself is live


def test_treasury_yields_are_live_only_for_the_lane_that_is_named_and_priced_from_the_contract(tmp_path, monkeypatch):
    """2026-11-04 (user): the four yield series are live, at $1 a bet, but only for a lane named in LIVE_LANES; they are priced from the contract's own band (no yield feed)."""
    s = settings(tmp_path, monkeypatch)
    for series in ("KX2YRRATE15M", "KX5YRRATE15M", "KX10YRRATE15M", "KX30YRRATE15M"):
        inst = cm.instrument_for(series)
        assert inst is not None and inst.live and cm.contract_priced(inst.symbol), series
        assert not s.is_live("some_unnamed_lane", series) or s.live_lanes is None, series
