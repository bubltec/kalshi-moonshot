import json
import math
import random
import time
from types import SimpleNamespace

from kalshi_moonshot import config, hotcheck, ticks, variants
from kalshi_moonshot.store import Store


def _feed(now, hot_series, step_hot=3e-4, step_calm=1e-4):
    """1 s ticks for 20 minutes on every crypto index; the named ones move three times their baseline step."""
    random.seed(6)
    ticks.history.clear()
    hotcheck._baseline.clear()
    for ser in hotcheck.SERIES:
        ix = variants.crypto_index(ser)
        hotcheck._baseline[ix] = 1e-4  # each index's normal one-second move
        p = 100.0
        for i in range(1200):
            p *= math.exp(random.gauss(0, step_hot if ser in hot_series else step_calm))
            ticks.history.note(ix, now - 1200 + i, p)


def test_the_hot_count_is_how_many_indexes_run_far_over_their_own_normal():
    now = time.time()
    _feed(now, set(hotcheck.SERIES[:6]))
    try:
        hot, seen, ratios = hotcheck.hot_count(now)
        assert seen == len(hotcheck.SERIES) and hot == 6
        assert all(ratios[x] > 2.0 for x in hotcheck.SERIES[:6]) and all(ratios[x] < 1.4 for x in hotcheck.SERIES[6:])
        _feed(now, set(hotcheck.SERIES[:2]))
        assert hotcheck.hot_count(now)[0] == 2
    finally:
        ticks.history.clear()
        hotcheck._baseline.clear()


def test_the_gate_sits_out_a_hot_market_only_with_hot_gate_on_and_the_shadow_logs_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    hotcheck._seen.clear()
    now = time.time()
    _feed(now, set(hotcheck.SERIES[:6]))
    shot = SimpleNamespace(ticker="KXSOL15M-X", lane="crypto_multiscale", side="yes", series="KXSOL15M")
    on, off = SimpleNamespace(hot_gate=True), SimpleNamespace(hot_gate=False)
    try:
        assert hotcheck.allows(off, now)[0] is True
        ok, why = hotcheck.allows(on, now)
        assert not ok and "hot market" in why
        hotcheck.shadow(s, shot, off, 0.40, now)
        hotcheck.shadow(s, shot, off, 0.41, now + 5)  # the same market side: once
        rows = [json.loads(x) for x in (tmp_path / "hot_shadow.jsonl").read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["hot"] == 6 and rows[0]["would_skip"] is True
        _feed(now, set(hotcheck.SERIES[:2]))
        assert hotcheck.allows(on, now)[0] is True  # two hot: the market is not hot
        shot2 = SimpleNamespace(ticker="KXSOL15M-Y", lane="crypto_multiscale", side="yes", series="KXSOL15M")
        hotcheck.shadow(s, shot2, off, 0.40, now)
        rows = [json.loads(x) for x in (tmp_path / "hot_shadow.jsonl").read_text().splitlines()]
        assert rows[1]["hot"] == 2 and rows[1]["would_skip"] is False
    finally:
        ticks.history.clear()
        hotcheck._baseline.clear()
        hotcheck._seen.clear()


def test_the_logged_buys_are_scored_against_the_settled_window_in_the_kept_and_skipped_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    now = time.time()
    for tk, result in (("KXSOL15M-A", "yes"), ("KXSOL15M-B", "no")):
        store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", (tk, "KXSOL15M", now - 300, 100.0, result, now))
    store.db.commit()
    sigs = [
        {"kind": "signal", "ts": now - 900, "ticker": "KXSOL15M-A", "lane": "crypto_multiscale", "side": "yes", "ask": 0.4, "hot": 6, "seen": 9, "own": 2.0, "would_skip": True},  # a hot market, the buy won
        {"kind": "signal", "ts": now - 900, "ticker": "KXSOL15M-B", "lane": "crypto_multiscale", "side": "yes", "ask": 0.4, "hot": 1, "seen": 9, "own": 1.0, "would_skip": False},  # a calm one, the buy lost
    ]
    (tmp_path / "hot_shadow.jsonl").write_text("\n".join(json.dumps(x) for x in sigs) + "\n")
    hotcheck._scored_at = 0.0
    new = hotcheck.run_score(s, store, now=now)
    got = {r["ticker"]: r for r in new}
    assert got["KXSOL15M-A"]["would_skip"] is True and got["KXSOL15M-A"]["net"] > 1.0
    assert got["KXSOL15M-B"]["would_skip"] is False and got["KXSOL15M-B"]["net"] < -1.0
    hotcheck._scored_at = 0.0
    assert hotcheck.run_score(s, store, now=now + 4000) == []


def _feed_family(now, family, hot_series, step_hot=3e-4, step_calm=1e-4):
    random.seed(7)
    ticks.history.clear()
    hotcheck._baseline.clear()
    for ser in hotcheck.FAMILIES[family]:
        ix = variants.crypto_index(ser) or variants.biquote_index(ser)
        hotcheck._baseline[ix] = 1e-4
        p = 100.0
        for i in range(1200):
            p *= math.exp(random.gauss(0, step_hot if ser in hot_series else step_calm))
            ticks.history.note(ix, now - 1200 + i, p)


def test_a_commodity_lane_is_judged_by_the_commodity_indexes_and_not_by_crypto(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    hotcheck._seen.clear()
    now = time.time()
    crypto_lane = SimpleNamespace(hot_gate=True, series=["KXBTC15M"])
    commodity_lane = SimpleNamespace(hot_gate=True, series=["KXGOLD15M", "KXWTI15M"])
    assert hotcheck.family_of(["KXBTC15M"]) == "crypto" and hotcheck.family_of("KXGOLD15M") == "commodity" and hotcheck.family_of("KX10YRRATE15M") is None
    assert hotcheck.hot_min("crypto") == 5 and hotcheck.hot_min("commodity") == 4
    try:
        _feed_family(now, "crypto", set(hotcheck.FAMILIES["crypto"][:6]))  # crypto is hot, no commodity index has ticks
        assert hotcheck.allows(crypto_lane, now)[0] is False
        assert hotcheck.allows(commodity_lane, now)[0] is True  # the commodity indexes cannot be read: nothing to sit out for
        hotcheck.shadow(s, SimpleNamespace(ticker="KXGOLD15M-X", lane="m", side="yes", series="KXGOLD15M"), commodity_lane, 0.4, now)
        assert not (tmp_path / "hot_shadow.jsonl").exists()  # and no crypto reading is logged against a commodity buy
        _feed_family(now, "commodity", set(hotcheck.FAMILIES["commodity"][:4]))  # four of the eight commodity indexes hot
        ok, why = hotcheck.allows(commodity_lane, now)
        assert not ok and "4 of 8 commodity indexes" in why
        assert hotcheck.allows(crypto_lane, now)[0] is True  # and crypto, with no ticks, is not judged by it
        _feed_family(now, "commodity", set(hotcheck.FAMILIES["commodity"][:3]))
        assert hotcheck.allows(commodity_lane, now)[0] is True
    finally:
        ticks.history.clear()
        hotcheck._baseline.clear()
        hotcheck._seen.clear()
