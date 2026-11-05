import itertools
import math

import pytest

from kalshi_moonshot import variants


def test_each_variety_is_read_from_the_cushion_the_slope_and_the_crossings():
    assert variants.classify(2.0, 0.9, 0) == "A"  # far and still moving away
    assert variants.classify(2.0, 0.0, 1) == "B"  # far and flat
    assert variants.classify(1.5, -0.8, 2) == "C"  # far but coming back
    assert variants.classify(0.4, -0.7, 1) == "D"  # near and approaching the strike
    assert variants.classify(0.4, 0.1, 0) == "E"  # near and flat
    assert variants.classify(0.4, 0.9, 3) == "F"  # near but pulling away
    assert variants.classify(2.0, 0.9, 4) == "G"  # four crossings is oscillation whatever the cushion


def _ticks(values, start=1000.0):
    return [start + k for k in range(len(values))], list(values)


def test_a_snapshot_reads_a_steady_cushion_as_far_and_flat_and_flips_with_the_side():
    wobble = [100.0 * (1 + 0.0004 * (1 if k % 2 else -1) * 0.1) + 0.2 for k in range(400)]  # 20 bp above a strike of 100, barely moving
    ts, v = _ticks(wobble)
    at = ts[-1]
    close = at + 300.0
    yes = variants.snapshot(ts, v, 100.0, "yes", at, close)
    assert yes is not None and yes["variety"] == "B" and yes["crosses"] == 0 and yes["z"] > 1.0
    no = variants.snapshot(ts, v, 100.0, "no", at, close)
    assert no is not None and no["z"] == pytest.approx(-yes["z"], rel=1e-9) and no["variety"] in ("D", "E", "F")


def test_a_snapshot_counts_the_crossings_and_refuses_thin_or_stale_history():
    ts, v = _ticks([100.0 + (0.05 if (k // 20) % 2 else -0.05) for k in range(400)])  # crosses the strike every 20 s
    snap = variants.snapshot(ts, v, 100.0, "yes", ts[-1], ts[-1] + 300.0)
    assert snap is not None and snap["variety"] == "G" and snap["crosses"] >= 4
    assert variants.snapshot(ts[:5], v[:5], 100.0, "yes", ts[4], ts[4] + 300.0) is None  # too few ticks
    assert variants.snapshot(ts, v, 100.0, "yes", ts[-1] + 60.0, ts[-1] + 360.0) is None  # the last tick is a minute old
    assert variants.snapshot(ts, v, 0.0, "yes", ts[-1], ts[-1] + 300.0) is None  # no strike
    assert not math.isnan(snap["z"])


def test_the_favorite_is_the_first_qualifying_side_between_minute_seven_and_the_last_two_minutes():
    close = 1000.0
    quotes = [
        (close - 600, 0.84, 0.85),  # before minute 7: too early
        (close - 470, 0.60, 0.62),  # no side in the band
        (close - 400, 0.14, 0.15),  # the NO side asks 0.86 with a 1c spread
        (close - 300, 0.86, 0.87),
        (close - 100, 0.90, 0.91),  # inside the last two minutes: too late
    ]
    side, ask, at, bid = variants.favorite_entry(quotes, close)
    assert side == "no" and ask == pytest.approx(0.86) and at == close - 400 and bid == pytest.approx(0.85)
    assert variants.favorite_entry([(close - 300, 0.80, 0.85)], close) is None  # a 5c spread is too wide
    assert variants.favorite_entry([(close - 300, 0.95, 0.96)], close) is None  # above the band
    assert variants.favorite_entry([], close) is None


def test_the_grid_makes_a_slow_and_a_fast_feed_read_alike():
    def path(t):  # one spot path: 1 bp wiggles that cross the strike of 100 every 20 s
        return 100.0 + (0.01 if (t // 20) % 2 else -0.01)

    fast = [(1000.0 + k, path(k)) for k in range(400)]  # a tick every second
    slow = [(1000.0 + k, path(k)) for k in range(0, 400, 3)]  # the same path, a tick every 3 s
    a = variants.snapshot([x for x, _ in fast], [y for _, y in fast], 100.0, "yes", 1399.0, 1699.0)
    b = variants.snapshot([x for x, _ in slow], [y for _, y in slow], 100.0, "yes", 1398.0, 1698.0)
    assert a is not None and b is not None
    assert a["variety"] == b["variety"] == "G" and abs(a["crosses"] - b["crosses"]) <= 1


def test_a_skip_list_names_a_variety_on_either_side_or_on_one():
    assert variants.skipped(("G", "A-yes"), "G", "no") and variants.skipped(("G", "A-yes"), "G", "yes")
    assert variants.skipped(("G", "A-yes"), "A", "yes") and not variants.skipped(("G", "A-yes"), "A", "no")
    assert not variants.skipped(("G", "A-yes"), "B", "yes")
    assert not variants.skipped(("G",), None, "yes")  # a variety that cannot be read is never skipped


def test_the_market_report_prints_every_class_with_its_flip(monkeypatch):
    def cell(n, wins, net, flip_wins, flip_net, flip_price):
        return {"n": n, "wins": wins, "net": net, "recent_n": 4, "recent_wins": 3, "recent_net": -2.0, "flip_wins": flip_wins, "flip_net": flip_net, "flip_price": flip_price, "recent_flip_net": 1.5}

    board = {
        ("crypto", "BTC", "yes", "F"): cell(40.0, 31.0, -12.0, 9.0, 8.0, 8.0),
        ("crypto", "ETH", "yes", "F"): cell(30.0, 24.0, -5.0, 6.0, -3.0, 6.0),
        ("commodity", "COPPER", "no", "G"): cell(20.0, 15.0, -9.0, 5.0, 14.0, 4.0),
    }
    monkeypatch.setattr(variants, "market_scoreboard", lambda *a, **k: board)
    text = "\n".join(variants.market_report(None, min_n=5))
    assert "crypto:" in text and "commodity:" in text and "F near, pulling away" in text and "G oscillating" in text
    assert "flip" in text and "+0.07" in text  # crypto F yes: (8.0 - 3.0) / 70 bets


def _rec(close, variety="F", side="yes", pnl=-1.0, cls="crypto", ticker=None):
    return {"ticker": ticker or f"T{close}{variety}{side}", "cls": cls, "side": side, "variety": variety, "close_ts": close, "pnl": pnl}


def test_the_ledger_judges_the_recent_stretch_against_the_older_record():
    led = variants.Ledger()
    now = 100 * 3600.0
    led.add([_rec(now - 20 * 3600 + k, "F", "yes", +0.5) for k in range(40)])  # a long record that paid
    led.add([_rec(now - 3600 + k, "F", "yes", -3.0) for k in range(6)])  # six bad recent bets: the blend is (6*-3 + 15*0.5)/21 = -0.5
    led.add([_rec(now - 3600 + k, "B", "yes", -3.0) for k in range(4)])  # too few recent bets to judge
    led.add([_rec(now - 3600 + k, "G", "no", -3.0) for k in range(6)])  # bad recent, and nothing older: no record to blend with, never skipped
    led.add([_rec(now - 3600 + k, "E", "no", -0.2) for k in range(6)] + [_rec(now - 20 * 3600 + k, "E", "no", +2.0, ticker=f"o{k}") for k in range(30)])  # a small dip against a good record
    assert led.skips("crypto", now, hours=6.0, prior_weight=15.0, min_loss=0.10, min_n=5) == ("F-yes",)
    assert led.skips("commodity", now) == ()


def test_the_ledger_forgets_what_is_older_than_its_keep_and_ignores_a_duplicate():
    led = variants.Ledger()
    led.add([_rec(1000.0, ticker="a"), _rec(1000.0, ticker="a")])
    assert len(led) == 1
    led.add([_rec(1000.0 + variants.LEDGER_KEEP + 10, ticker="b")])
    assert len(led) == 1 and "a" not in led.seen


def test_a_custom_variety_comes_from_a_file_and_is_checked(tmp_path):
    from types import SimpleNamespace

    s = SimpleNamespace(data_dir=tmp_path)
    (tmp_path / variants.CUSTOM_FILE).write_text(
        '[[variety]]\nletter="K"\nname="near and speeding up"\nwhen="z < 1 and vol_ratio > 1.5"\n'
        '[[variety]]\nletter="J"\nname="bad"\nwhen="__import__(1)"\n'
        '[[variety]]\nletter="B"\nname="reuses a built-in letter"\nwhen="z > 0"\n')
    variants._custom_mtime.clear()
    variants.load_custom(s)
    try:
        assert variants.NAMES["K"] == "near and speeding up" and "J" not in variants.NAMES and variants.NAMES["B"] == "far, flat"
        assert variants.classify_features({"z": 0.4, "slope": 0.0, "crosses": 0, "vol_ratio": 2.0, "since_cross": 300.0}) == "K"
        assert variants.classify_features({"z": 0.4, "slope": 0.0, "crosses": 0, "vol_ratio": 1.0, "since_cross": 300.0}) == "E"  # no custom test holds: the built-in letter
        assert variants.skipped(("K-yes",), "K", "yes") and not variants.skipped(("K-yes",), "K", "no")
    finally:
        (tmp_path / variants.CUSTOM_FILE).unlink()
        variants._custom_mtime.clear()
        variants.load_custom(s)
    assert "K" not in variants.NAMES


def test_the_snapshot_carries_the_features_a_custom_variety_can_test():
    wobble = [100.0 + 0.2 + 0.0004 * (k % 2) for k in range(400)]
    ts, v = _ticks(wobble)
    snap = variants.snapshot(ts, v, 100.0, "yes", ts[-1], ts[-1] + 300.0)
    assert snap is not None and set(variants.FEATURES) <= set(snap) and snap["since_cross"] == variants.LOOK and snap["vol_ratio"] == pytest.approx(1.0, abs=0.3)


def test_the_tick_history_answers_a_window_from_memory_and_says_when_it_cannot():
    from kalshi_moonshot import ticks

    h = ticks.TickHistory(keep=100.0)
    for k in range(10):
        h.note("IX", 1000.0 + k, 50.0 + k)
    h.note("IX", 1005.0, 99.0)  # a late tick is dropped, the series stays ordered
    assert h.window("IX", 1002.0, 1004.0) == ([1002.0, 1003.0, 1004.0], [52.0, 53.0, 54.0])
    assert h.window("IX", 900.0, 1004.0) is None  # the history starts at 1000: the tape must answer
    h.warm("IX", [(900.0 + k, 40.0) for k in range(100)], 900.0)
    got = h.window("IX", 900.0, 1001.0)
    assert got is not None and got[0][0] == 900.0 and got[0][-1] == 1001.0 and got[1][-1] == 51.0 and len(got[0]) == 102
    assert h.first_at_or_after("IX", 1003.5, 2.0) == 54.0 and h.first_at_or_after("IX", 1100.0, 2.0) is None
    for k in range(10, 400):
        h.note("IX", 1000.0 + k, 1.0)
    assert h.window("IX", 1000.0, 1400.0) is None and h.size("IX") < 400  # old ticks were trimmed away


def test_a_crypto_variety_is_read_from_memory_against_the_strike(monkeypatch):
    from types import SimpleNamespace

    from kalshi_moonshot import ticks

    ticks.history.clear()
    index = variants.crypto_index("KXBTC15M")
    now = 5000.0
    for k in range(341):
        ticks.history.note(index, now - 340 + k, 100.2 + 0.001 * (k % 2))  # 20 bp over a strike of 100, steady
    monkeypatch.setattr(variants.db, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the tape must not be read")))
    s = SimpleNamespace(tape_path="x", data_dir=__import__("pathlib").Path("/nonexistent"))
    try:
        assert variants.variety_now(s, "KXBTC15M", "yes", 300.0, now=now, strike=100.0) == "B"
        assert variants.variety_now(s, "KXBTC15M", "yes", 300.0, now=now) is None  # no strike, no variety
    finally:
        ticks.history.clear()


def test_the_ledger_scores_settled_favorites_saves_them_and_reloads_after_a_restart(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, db
    from kalshi_moonshot.store import Store

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    tape.execute("CREATE TABLE IF NOT EXISTS index_ticks (ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quote_avg REAL, src_ms INTEGER)")
    now = _time.time()
    close = now - 1200.0
    index = variants.crypto_index("KXBTC15M")
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", ("KXBTC15M-1", "KXBTC15M", close, 100.0, "yes", now))
    store.db.commit()
    tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (close - 300.0, "KXBTC15M-1", 0.84, 0.85))
    for k in range(400):  # 20 bp over the strike, steady, through the whole window
        tape.execute("INSERT INTO index_ticks (ts, index_id, value) VALUES (?,?,?)", (close - 700.0 + k * 2.0, index, 100.2 + 0.0004 * (k % 2)))
    tape.commit()
    variants.ledger.__init__()
    assert variants.refresh_ledger(s, now) == 1
    row = variants.ledger.rows[0]
    assert row["variety"] == "B" and row["won"] and row["side"] == "yes" and row["pnl"] > 0
    saved = Store(s.db_path).db.execute("SELECT variety, won, z FROM favorite_cases").fetchall()
    assert len(saved) == 1 and saved[0]["variety"] == "B" and saved[0]["won"] == 1
    variants.ledger.__init__()  # a restart: the table is reloaded, nothing is scored twice
    assert variants.refresh_ledger(s, now) == 0 and len(variants.ledger) == 1
    variants.ledger.__init__()


def test_the_mid_history_counts_crossings_of_50c_and_says_when_it_cannot():
    from kalshi_moonshot import ticks

    h = ticks.MidHistory()
    now = 10_000.0
    for k in range(0, 320, 2):  # a mid that sits at 0.70 the whole time
        h.note("KX-CALM", now - 320 + k, 0.69, 0.71)
    assert h.crossings("KX-CALM", now) == 0
    for k in range(0, 320, 2):  # a mid that swings across 50c every 40 s
        up = (k // 40) % 2 == 0
        h.note("KX-CHOP", now - 320 + k, 0.55 if up else 0.40, 0.59 if up else 0.44)
    assert h.crossings("KX-CHOP", now) >= 6
    for k in range(0, 60, 2):  # only a minute of history
        h.note("KX-NEW", now - 60 + k, 0.69, 0.71)
    assert h.crossings("KX-NEW", now) is None and h.crossings("KX-NONE", now) is None
    h.note("KX-ZERO", now - 400, 0.0, 0.0)
    assert h.crossings("KX-ZERO", now) is None


def test_a_lane_with_max_crossings_buys_only_a_calm_chart_and_a_named_paper_lane_runs_on_a_paper_host(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, lanes, regime, runner, ticks
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_LANES", "false")
    monkeypatch.setenv("LIVE_LANES", "commodity_energy")
    monkeypatch.setenv("PAPER_LANE_NAMES", "guarded")
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KX30YRRATE15M']\nmax_crossings=0\n[lane.other]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXBTC15M']\n")
    s = config.load()
    specs = lanes.lane_specs(s)
    assert specs["guarded"].enabled and not specs["other"].enabled and not s.is_live("guarded", "KX30YRRATE15M")  # named: runs, on paper; the other lane stays off
    store = Store(s.db_path)
    regime.reset()
    ticks.mids.clear()
    now = _time.time()
    shot = LongShot("KX30YRRATE15M-9", "t", "", 0.80, 0.82, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="guarded", series="KX30YRRATE15M", side="yes")
    try:
        out = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert out.action == "pass" and "crossings unknown" in out.note  # no history: no buy
        for k in range(0, 320, 2):
            ticks.mids.note("KX30YRRATE15M-9", now - 320 + k, 0.79, 0.81)
        calm = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert "choppy chart" not in calm.note and "crossings unknown" not in calm.note and "[0 crossings]" in calm.note
        ticks.mids.clear()
        for k in range(0, 320, 2):
            up = (k // 40) % 2 == 0
            ticks.mids.note("KX30YRRATE15M-9", now - 320 + k, 0.62 if up else 0.40, 0.66 if up else 0.44)
        chop = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert chop.action == "pass" and "choppy chart" in chop.note
    finally:
        ticks.mids.clear()


def _fav(close, coin, ask, pnl, cls="commodity"):
    return {"ticker": f"{coin}{close}{ask}", "cls": cls, "coin": coin, "side": "yes", "variety": "E", "close_ts": close, "ask": ask, "pnl": pnl}


def test_the_ledger_skips_a_market_that_loses_against_its_class_and_a_losing_price_band():
    led = variants.Ledger()
    now = 100 * 3600.0
    for k in range(40):  # Brent pays, palladium loses, gold sits at the class mean
        led.add([_fav(now - 3600 - k * 60, "BRENT", 0.83, +0.6), _fav(now - 3600 - k * 60 - 7, "PALLADIUM", 0.83, -1.0), _fav(now - 3600 - k * 60 - 13, "GOLD", 0.83, -0.2)])
    why = led.market_skip("commodity", "PALLADIUM", 0.83, now)
    assert why is not None and "PALLADIUM" in why and "lost" in why
    assert led.market_skip("commodity", "BRENT", 0.83, now) is None and led.market_skip("commodity", "GOLD", 0.83, now) is None
    assert led.market_skip("crypto", "PALLADIUM", 0.83, now) is None  # judged inside its own class
    assert led.market_skip("commodity", "NATGAS", 0.83, now) is None  # an unseen market is not skipped
    assert led.market_skip("commodity", "PALLADIUM", 0.83, now + 100 * 3600.0) is None  # nothing left in the stretch: no verdict
    band = variants.Ledger()  # a market that is fine overall but loses in one price band
    for k in range(30):
        band.add([_fav(now - 3600 - k * 60, "SILVER", 0.80, -1.5), _fav(now - 3600 - k * 60 - 7, "SILVER", 0.85, +1.5), _fav(now - 3600 - k * 60 - 13, "SILVER", 0.90, +1.5)])
    assert band.market_skip("commodity", "SILVER", 0.80, now, min_loss=5.0) is not None and "priced .78-.82" in band.market_skip("commodity", "SILVER", 0.80, now, min_loss=5.0)
    assert band.market_skip("commodity", "SILVER", 0.85, now, min_loss=5.0) is None
    assert variants.coin_of("KXPALLADIUM15M") == "PALLADIUM" and variants.coin_of("KXBTC15M") == "BTC" and variants.coin_of("KX30YRRATE15M") == "30YRRATE"
    assert [variants.market_band(a) for a in (0.78, 0.82, 0.86, 0.87, 0.92)] == [".78-.82", ".82-.87", ".82-.87", ".87+", ".87+"]


def test_the_volatility_regime_is_read_from_memory_and_compared_with_the_coin_normal():
    from kalshi_moonshot import ticks, volregime

    ticks.history.clear()
    reg = volregime.VolRegime()
    now = 1_000_000.0
    calm, wild = 0.0002, 0.0008  # one-minute returns
    px = 100.0
    for k in range(0, 7 * 3600, 1):  # 7 h of 1 Hz ticks: the first hour calm, then the last 6 h wild
        t = now - 7 * 3600 + k
        if k % 60 == 0:
            px *= 1 + (wild if (k // 60) % 2 else -wild) * (1 if k >= 3600 else calm / wild)
        ticks.history.note("IX", t, px)
    got = ticks.history.window("IX", now - 6 * 3600 - 120, now)
    vol = volregime.hourly_vol_bp(volregime.minute_values(got[0], got[1]), int(now // 60))
    assert vol is not None and 40 < vol < 90  # about 0.0008 a minute -> 0.0008 * sqrt(60) = 62 bp an hour
    reg.set("TEST", vol, vol / 1.5)
    assert reg.rel("TEST") == pytest.approx(1.5) and reg.rel("NONE") is None
    assert volregime.hourly_vol_bp({}, 100) is None  # no history: unknown, never zero
    ticks.history.clear()


def test_a_lane_buys_only_in_its_volatility_regime_and_behind_the_strike(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants, volregime
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.05,0.5]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nmin_vol_rel=1.3\nmax_cushion_z=-1.0\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    volregime.regime = volregime.VolRegime()
    monkeypatch.setattr(runner, "volregime", volregime, raising=False)
    now = _time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-9", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    index = variants.crypto_index("KXBTC15M")
    for k in range(176):  # the index sits 20 bp ABOVE the strike, steady: a NO bet is 'behind' by many typical moves
        ticks.history.note(index, now - 350.0 + k * 2.0, 100.20 + 0.0004 * (k % 2))
    shot = LongShot("KXBTC15M-9", "t", "", 0.11, 0.12, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="guarded", series="KXBTC15M", side="no")
    try:
        out = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert out.action == "pass" and "volatility regime unknown" in out.note  # no reading: no buy
        volregime.regime.set("BTC", 70.0, 50.0)  # 1.4x its normal
        ok = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert "[vol 1.40x]" in ok.note and "[z -" in ok.note and "above the lane" not in ok.note
        volregime.regime.set("BTC", 55.0, 50.0)  # 1.1x: not wild
        calm = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert calm.action == "pass" and "outside the lane" in calm.note
        volregime.regime.set("BTC", 70.0, 50.0)
        yes = LongShot("KXBTC15M-9", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="guarded", series="KXBTC15M", side="yes")
        ahead = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert ahead.action == "pass" and "above the lane" in ahead.note  # the side that leads is not the underdog
    finally:
        ticks.history.clear()
        volregime.regime = volregime.VolRegime()


def test_the_record_only_crypto_series_have_an_index_but_never_reach_the_ledger_or_the_gates():
    from kalshi_moonshot import kalshiws

    for series, index in (("KXADA15M", "ADAUSD_RTI"), ("KXBCH15M", "BCHUSD_RTI"), ("KXBNB15M", "BNBUSD_RTI"), ("KXHYPE15M", "HYPEUSD_RTI"), ("KXNEAR15M", "NEARUSD_RTI"), ("KXZEC15M", "ZECUSD_RTI")):
        assert kalshiws.index_for_series(series) == index and series in kalshiws.RECORD_ONLY
    assert kalshiws.index_for_series("KXTON15M") is None and "KXTON15M" in kalshiws.RECORD_ONLY  # no spot index: its quotes only
    assert not ({"KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"} & kalshiws.RECORD_ONLY)
    assert "ADA" not in variants.COINS.values()  # the volatility regime only measures the coins that are traded


def test_the_volatility_regime_updates_from_a_history_warmed_for_eight_hours(tmp_path):
    from types import SimpleNamespace

    from kalshi_moonshot import ticks, volregime

    ticks.history.clear()
    now = 2_000_000.0
    rows = []
    px = 100.0
    for k in range(8 * 3600):  # 8 h of 1 Hz ticks with a one-minute zigzag
        if k % 60 == 0:
            px *= 1.0006 if (k // 60) % 2 else 0.9994
        rows.append((now - 8 * 3600 + k, px))
    ticks.history.warm("IX", rows, now - 8 * 3600)
    reg = volregime.VolRegime()
    reg._base_at = now  # the baseline is not due: only the current figure is redone
    reg.set("BTC", None, 40.0)
    ratios = reg.update(SimpleNamespace(), {"BTC": "IX"}, now)
    assert ratios["BTC"] is not None and 1.0 < ratios["BTC"] < 3.0
    ticks.history.clear()
    ticks.history.warm("IX", [r for r in rows if r[0] > now - 6 * 3600], now - 6 * 3600)  # a 6-hour load cannot cover 6 h 2 min
    reg2 = volregime.VolRegime()
    reg2._base_at = now
    reg2.set("BTC", None, 40.0)
    assert reg2.update(SimpleNamespace(), {"BTC": "IX"}, now)["BTC"] is None
    ticks.history.clear()


def test_a_live_markets_strike_comes_from_the_listing_in_memory_not_from_the_windows_table(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants, volregime
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    st_ = ticks.Strikes(keep=2)
    st_.note("A", 1.0)
    st_.note("B", 2.0)
    st_.note("C", 3.0)
    st_.note("Z", 0.0)  # no strike
    assert st_.get("A") is None and st_.get("B") == 2.0 and st_.get("C") == 3.0 and st_.get("Z") is None

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.05,0.5]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nmin_vol_rel=1.3\nmax_cushion_z=-1.0\n")
    s = config.load()
    store = Store(s.db_path)  # no windows row at all: the market has not settled
    regime.reset()
    ticks.history.clear()
    ticks.strikes.clear()
    volregime.regime = volregime.VolRegime()
    volregime.regime.set("BTC", 70.0, 50.0)
    now = _time.time()
    index = variants.crypto_index("KXBTC15M")
    for k in range(176):
        ticks.history.note(index, now - 350.0 + k * 2.0, 100.20 + 0.0004 * (k % 2))
    shot = LongShot("KXBTC15M-9", "t", "", 0.11, 0.12, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="guarded", series="KXBTC15M", side="no")
    try:
        out = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert out.action == "pass" and "cushion unreadable" in out.note
        ticks.strikes.note("KXBTC15M-9", 100.0)
        ok = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert "[z -" in ok.note and "unreadable" not in ok.note
    finally:
        ticks.history.clear()
        ticks.strikes.clear()
        volregime.regime = volregime.VolRegime()


def test_an_underdog_lane_skips_a_far_and_flat_favorite(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants, volregime
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.05,0.5]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nskip_other_varieties=['B']\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    ticks.strikes.clear()
    volregime.regime = volregime.VolRegime()
    now = _time.time()
    ticks.strikes.note("KXBTC15M-9", 100.0)
    index = variants.crypto_index("KXBTC15M")
    shot = LongShot("KXBTC15M-9", "t", "", 0.11, 0.12, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="guarded", series="KXBTC15M", side="no")
    try:
        for k in range(176):  # the index sits 20 bp over the strike and barely moves: the YES favorite is far and flat (B)
            ticks.history.note(index, now - 350.0 + k * 2.0, 100.20 + 0.0004 * (k % 2))
        flat = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert flat.action == "pass" and "variety B" in flat.note and "does not buy against it" in flat.note
        ticks.history.clear()
        for k in range(176):  # the same distance but still running away from the strike (A): bought
            ticks.history.note(index, now - 350.0 + k * 2.0, 100.02 + 0.0012 * k + 0.0004 * (k % 2))
        run = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert "does not buy against it" not in run.note and "[favorite " in run.note
    finally:
        ticks.history.clear()
        ticks.strikes.clear()


def test_a_gradual_erosion_is_its_own_variety_and_a_spike_or_a_steady_cushion_is_not():
    ts = [1000.0 + k for k in range(401)]
    steady = [100.45 + 0.02 * (1 if k % 2 else -1) for k in range(401)]  # a near cushion that does not move
    snap = variants.snapshot(ts, steady, 100.0, "yes", ts[-1], ts[-1] + 150.0)
    assert snap is not None and snap["variety"] != "H" and abs(snap["d120"]) < 0.5
    # flat for three minutes, then a steady slide in the last two: the cushion shrinks without a single jump
    slide = [100.45 + 0.02 * (1 if k % 2 else -1) - (0.30 * (k - 281) / 120.0 if k > 281 else 0.0) for k in range(401)]
    snap = variants.snapshot(ts, slide, 100.0, "yes", ts[-1], ts[-1] + 150.0)
    assert snap is not None and snap["d120"] <= variants.GRADUAL_D120 and snap["erosion"] >= variants.GRADUAL_EROSION
    assert snap["variety"] == "H" and "H" in variants.NAMES
    assert variants.classify_features({"z": 0.5, "slope": 0.0, "crosses": 0, "vol_ratio": 1.0, "since_cross": 300.0, "d60": -0.2, "d120": -0.1, "erosion": 0.1}) == "E"  # no erosion: stays E
    assert variants.classify_features({"z": 1.5, "slope": 0.0, "crosses": 0, "vol_ratio": 1.0, "since_cross": 300.0, "d60": -0.9, "d120": -0.9, "erosion": 0.9}) == "B"  # far and flat keeps its letter: H only subdivides E


def test_a_commodity_lane_stands_aside_in_a_wild_regime_and_a_commodity_has_its_own_look_back(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kalshi_moonshot import config, regime, runner, ticks, variants, volregime
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    # a four-hour look-back is enough for a commodity; the same history is too short for a six-hour crypto reading
    ticks.history.clear()
    now = 3_000_000.0
    rows, px = [], 100.0
    for k in range(5 * 3600):
        if k % 60 == 0:
            px *= 1.0005 if (k // 60) % 2 else 0.9995
        rows.append((now - 5 * 3600 + k, px))
    ticks.history.warm("BQ_X", rows, now - 5 * 3600)
    reg = volregime.VolRegime()
    reg._base_at = now
    reg.set("GOLD", None, 20.0)
    reg.set("BTC", None, 20.0)
    got = reg.update(SimpleNamespace(), {"GOLD": ("BQ_X", 4 * 3600.0), "BTC": "BQ_X"}, now)
    assert got["GOLD"] is not None and got["BTC"] is None
    ticks.history.clear()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXGOLD15M']\nmax_vol_rel=1.3\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    volregime.regime = volregime.VolRegime()
    shot = LongShot("KXGOLD15M-9", "t", "", 0.85, 0.86, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="guarded", series="KXGOLD15M", side="yes")
    try:
        assert variants.coin_of("KXGOLD15M") == "GOLD"
        unknown = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert unknown.action == "pass" and "regime unknown" in unknown.note
        volregime.regime.set("GOLD", 30.0, 20.0)  # 1.5x
        wild = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert wild.action == "pass" and "outside the lane" in wild.note
        volregime.regime.set("GOLD", 22.0, 20.0)  # 1.1x
        calm = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert "[vol 1.10x]" in calm.note and "outside the lane" not in calm.note
    finally:
        volregime.regime = volregime.VolRegime()


def _vrow(cls, vol_rel, pnl, flip=0.0, ask=0.85):
    return {"cls": cls, "vol_rel": vol_rel, "pnl": pnl, "flip_pnl": flip, "ask": ask}


def test_the_tuner_finds_the_level_above_which_favorites_lost_and_the_level_from_which_the_underdog_paid(tmp_path):
    from kalshi_moonshot import volregime

    rows = ([_vrow("crypto", 0.7, +0.3, flip=-3.0) for _ in range(60)]                      # calm: favorites pay, the underdog does not
            + [_vrow("crypto", 1.15, +0.1, flip=-1.0) for _ in range(50)]                    # in between: a little positive
            + [_vrow("crypto", 1.6, -0.9, flip=+2.0) for _ in range(50)]                     # wild: favorites lose, the underdog pays
            + [_vrow("commodity", 0.8, +0.1) for _ in range(80)] + [_vrow("commodity", 1.4, -0.4) for _ in range(45)]
            + [_vrow("crypto", None, -9.0) for _ in range(30)])                              # no ratio saved: ignored
    mx, mn = volregime.tune_thresholds(rows)
    assert mx["crypto"] == 1.3 and mn["crypto"] == 1.3 and mx["commodity"] == 1.1  # levels 1.2-1.5 (crypto) and 0.9-1.3 (commodity) skip the same favorites: the middle of each run
    assert "commodity" not in mn  # no underdog evidence there: the lane's static setting stands
    mx2, _ = volregime.tune_thresholds(rows, previous_max={"crypto": 1.0})
    assert mx2["crypto"] == pytest.approx(1.15)  # half way from yesterday's 1.0 to today's 1.3
    assert volregime.tune_thresholds([_vrow("crypto", 1.6, -0.9) for _ in range(10)]) == ({}, {})  # too few favorites: no tuned level


def test_the_tuner_sets_no_ceiling_when_every_favorite_lost_alike():
    from kalshi_moonshot import volregime

    # 2026-11-05: every favorite lost about the same whatever the volatility; the old rule picked the lowest level (0.8) and the lanes stopped buying
    rows = [_vrow(cls, 0.5 + 0.05 * k, -0.4) for cls in ("crypto", "commodity") for k in range(30) for _ in range(5)]
    assert volregime.tune_thresholds(rows)[0] == {}
    # a ceiling that would leave under 40% of the favorites to buy is refused too, even when the ones above it lost more
    rows = [_vrow("crypto", 0.9, -0.1) for _ in range(40)] + [_vrow("crypto", 1.2, -0.9) for _ in range(120)]
    assert volregime.tune_thresholds(rows)[0] == {}


def test_a_lane_with_auto_vol_gate_follows_the_tuned_level(tmp_path, monkeypatch):
    from kalshi_moonshot import config, regime, runner, variants, volregime
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXGOLD15M']\nmax_vol_rel=1.3\nauto_vol_gate=true\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    volregime.regime = volregime.VolRegime()
    volregime.thresholds = volregime.Thresholds()
    volregime.regime.set("GOLD", 24.0, 20.0)  # 1.2x
    shot = LongShot("KXGOLD15M-9", "t", "", 0.85, 0.86, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="guarded", series="KXGOLD15M", side="yes")
    try:
        assert variants.coin_of("KXGOLD15M") == "GOLD"
        ok = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert "[vol 1.20x]" in ok.note  # no tuned level yet: the static 1.3 applies
        volregime.thresholds.stand_aside["commodity"] = 1.1  # tuned tighter
        tight = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert tight.action == "pass" and "outside the lane" in tight.note
        volregime.thresholds.save(tmp_path)
        again = volregime.Thresholds()
        again.load(tmp_path)  # a restart keeps the tuning
        assert again.get_max("commodity", 1.3) == 1.1 and again.get_max("crypto", 1.3) == 1.3
    finally:
        volregime.regime = volregime.VolRegime()
        volregime.thresholds = volregime.Thresholds()


def test_a_precious_lane_skips_a_bet_where_the_spot_and_the_corrected_center_disagree_about_the_strike(tmp_path, monkeypatch):
    from kalshi_moonshot import commodities, config, regime, runner, ticks
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    pricing = commodities.Pricing(center=4059.52, spot=4060.95, basis=-0.0004, sigma=0.0011, sigma_market=None, points=6, r2=0.0, age_s=4.0, anchor="window")
    assert commodities.parse_spot_center(pricing.note(0.2, 0.04)) == (4060.95, 4059.52)  # the note format and the parser stay in step
    assert commodities.parse_spot_center("no model: priced at the 0.82 asked") is None

    class Fixed:
        def __init__(self, why):
            self.why = why

        def estimate(self, client, settings, shot):
            return Estimate(0.2, self.why)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXGOLD15M']\nrequire_spot_center_agree=true\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.strikes.clear()
    ticks.strikes.note("KXGOLD15M-9", 4059.87)
    no = LongShot("KXGOLD15M-9", "t", "", 0.79, 0.80, 160.0 / 3600.0, "2099-01-01T00:00:00Z", 160.0 / 3600.0, lane="guarded", series="KXGOLD15M", side="no")
    try:
        # spot 4060.95 is ABOVE the strike (a NO bet needs it below); the corrected center 4059.52 is below: the bet leans on the offset
        why = pricing.note(0.2, 0.04)
        out = runner.evaluate(s, None, None, store, no, estimators={"market": Fixed(why)})
        assert out.action == "pass" and "do not both sit" in out.note
        both = pricing.note(0.2, 0.04).replace("spot 4060.95", "spot 4059.40")  # spot and center both below the strike
        ok = runner.evaluate(s, None, None, store, no, estimators={"market": Fixed(both)})
        assert "do not both sit" not in ok.note
        yes = LongShot("KXGOLD15M-9", "t", "", 0.79, 0.80, 160.0 / 3600.0, "2099-01-01T00:00:00Z", 160.0 / 3600.0, lane="guarded", series="KXGOLD15M", side="yes")
        wrong = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed(why)})
        assert wrong.action == "pass" and "do not both sit" in wrong.note  # spot above, center below: a YES bet disagrees too
        plain = runner.evaluate(s, None, None, store, no, estimators={"market": Fixed("no model: priced at the 0.82 asked")})
        assert "do not both sit" not in plain.note  # no pair in the note: unchanged
        (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=8.0\nseries=['KXGOLD15M']\nrequire_spot_center_agree=true\ndisagree_stake_factor=0.25\nmax_bet_pct=1.0\nmax_exposure_pct=1.0\n")
        quarter = runner.evaluate(config.load(), None, None, store, no, estimators={"market": Fixed(why)})
        assert "[stake x0.25:" in quarter.note and "the bet is skipped" not in quarter.note  # the bet is played at a quarter of the stake: the note says so and the gate did not turn it into a pass for that reason
        assert not ("do not both sit" in quarter.note and quarter.note.startswith("the spot"))  # the skip wording is only for the skip mode
    finally:
        ticks.strikes.clear()


def _path(points, step=1.0, start=1000.0):
    """A price path through the (price, seconds) waypoints, one tick a second."""
    ts, vs = [], []
    t = start
    for (p0, n), (p1, _) in itertools.pairwise(points):
        for k in range(int(n)):
            ts.append(t)
            vs.append(p0 + (p1 - p0) * k / n + (0.01 if k % 2 else -0.01))
            t += step
    return ts, vs


def test_swing_features_see_the_whole_window_and_the_relapse_shape():
    # up, down, up, down over 600 s above a strike of 100: the last three legs are down, up, down
    ts, vs = _path([(101.0, 150), (103.0, 150), (101.5, 150), (103.0, 150), (101.2, 0)])
    f = variants.swing_features(ts, vs, 100.0, "yes", ts[0], ts[-1] + 1.0)
    assert f is not None
    assert f["sw_relapse"] == 1 and f["sw_last_dir"] == -1 and f["sw_cross"] == 0 and f["sw_onside"] == 1.0
    assert f["sw_legs"] >= 3 and 0.0 < f["sw_retrace"] < 1.0
    no = variants.swing_features(ts, vs, 100.0, "no", ts[0], ts[-1] + 1.0)
    assert no is not None and no["sw_onside"] == 0.0


def test_swing_features_are_none_when_the_history_does_not_cover_the_window():
    ts, vs = _path([(101.0, 100), (102.0, 100), (101.0, 0)])
    assert variants.swing_features(ts, vs, 100.0, "yes", ts[0] - 500.0, ts[-1]) is None


def test_a_lane_waits_for_a_cushion_and_refuses_an_oscillating_spot(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.cushioned]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nmin_cushion_z=1.0\nmax_spot_crosses=4\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    now = _time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-8", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    index = variants.crypto_index("KXBTC15M")
    yes = LongShot("KXBTC15M-8", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="cushioned", series="KXBTC15M", side="yes")
    try:
        for k in range(176):  # steady, 20 bp above the strike: a big cushion for YES
            ticks.history.note(index, now - 350.0 + k * 2.0, 100.20 + 0.0004 * (k % 2))
        far = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert far.action == "bet" and "[z +" in far.note
        ticks.history.clear()
        for k in range(176):  # barely above the strike, flat: a cushion under one typical move
            ticks.history.note(index, now - 350.0 + k * 2.0, 100.0005 + 0.0004 * (k % 2))
        near = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert near.action == "pass" and "waits for a bigger cushion" in near.note
        ticks.history.clear()
        for k in range(176):  # weaving across the strike every few seconds
            ticks.history.note(index, now - 350.0 + k * 2.0, 100.20 if (k // 8) % 2 == 0 else 99.80)
        weave = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert weave.action == "pass" and ("crossed the strike" in weave.note or "waits for a bigger cushion" in weave.note)
    finally:
        ticks.history.clear()


def test_scale_letter_reads_the_variety_at_a_look_back_and_needs_the_history():
    ts = [1000.0 + k for k in range(4000)]
    flat_above = [100.20 + 0.0004 * (k % 2) for k in range(4000)]
    sn = variants.scale_letter(ts, flat_above, 100.0, "yes", ts[-1], ts[-1] + 400.0, 300.0)
    assert sn is not None and sn["variety"] == "B" and sn["z"] > 1.0 and sn["crosses"] == 0  # far and flat
    weave = [100.20 if (k // 20) % 2 == 0 else 99.80 for k in range(4000)]
    sn = variants.scale_letter(ts, weave, 100.0, "yes", ts[-1], ts[-1] + 400.0, 300.0)
    assert sn is not None and sn["variety"] == "G"  # crossing every 20 s: oscillating at five minutes
    assert variants.scale_letter(ts[:200], flat_above[:200], 100.0, "yes", ts[199], ts[199] + 400.0, 3600.0) is None  # not an hour of history


def test_a_lane_with_an_ms_rule_buys_only_when_every_scale_matches(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.ms]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nms_rule=['60m:B','5m:B']\n[lane.ms_no]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nms_rule=['60m:D']\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    now = _time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    index = variants.crypto_index("KXBTC15M")
    for k in range(1950):  # 3900 s of a steady price 20 bp above the strike
        ticks.history.note(index, now - 3900.0 + k * 2.0, 100.20 + 0.0004 * (k % 2))
    yes = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ms", series="KXBTC15M", side="yes")
    no = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ms_no", series="KXBTC15M", side="yes")
    try:
        ok = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert ok.action == "bet" and "[ms 60m B 5m B]" in ok.note
        miss = runner.evaluate(s, None, None, store, no, estimators={"market": Fixed()})
        assert miss.action == "pass" and "do not match" in miss.note
        ticks.history.clear()
        none = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert none.action == "pass" and "unreadable" in none.note  # no history: never a buy
    finally:
        ticks.history.clear()


def test_broad_trend_is_the_average_hour_move_of_the_five_coins_and_unknown_without_history():
    import time as _time

    from kalshi_moonshot import ticks

    ticks.history.clear()
    variants._broad_memo = (0, None)
    now = _time.time()
    assert variants.broad_trend_now(now) is None  # no history: unknown, never zero
    for k, ser in enumerate(variants.BROAD_SERIES):
        index = variants.crypto_index(ser)
        for j in range(1900):  # about 3,800 s: this coin falls (2 + k) percent over the last hour
            tt = now - 3800.0 + j * 2.0
            frac = max(0.0, (tt - (now - 3600.0)) / 3600.0)
            ticks.history.note(index, tt, 100.0 * (1.0 - (0.02 + 0.01 * k) * frac))
    variants._broad_memo = (0, None)
    got = variants.broad_trend_now(now)
    assert got is not None and got == pytest.approx(-4.0, abs=0.3)  # (2+3+4+5+6)/5
    ticks.history.clear()
    variants._broad_memo = (0, None)


def test_a_trend_lane_buys_the_trend_side_only_in_a_market_wide_move(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.trend]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\ntrend_down_max=-1.0\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    variants._broad_memo = (0, None)
    now = _time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-6", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    no = LongShot("KXBTC15M-6", "t", "", 0.55, 0.56, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="trend", series="KXBTC15M", side="no")
    yes = LongShot("KXBTC15M-6", "t", "", 0.55, 0.56, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="trend", series="KXBTC15M", side="yes")
    try:
        assert runner.evaluate(s, None, None, store, no, estimators={"market": Fixed()}).action == "pass"  # no history: unreadable, never a buy
        for ser in variants.BROAD_SERIES:  # every coin down 3% over the last hour
            index = variants.crypto_index(ser)
            for j in range(1900):
                tt = now - 3800.0 + j * 2.0
                frac = max(0.0, (tt - (now - 3600.0)) / 3600.0)
                ticks.history.note(index, tt, 100.0 * (1.0 - 0.03 * frac))
        variants._broad_memo = (0, None)
        ok = runner.evaluate(s, None, None, store, no, estimators={"market": Fixed()})
        assert ok.action == "bet" and "[trend60 -3.0%]" in ok.note
        wrong = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert wrong.action == "pass" and "buys NO while" in wrong.note
    finally:
        ticks.history.clear()
        variants._broad_memo = (0, None)


def test_a_lane_refuses_a_bet_when_its_own_model_chance_is_far_under_the_price(tmp_path, monkeypatch):
    from kalshi_moonshot import config, regime, runner
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.90, "fixed")  # the model says the YES pays 90% of the time

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.edge]\nestimator='market'\nprice=[0.05,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nmin_model_edge=-0.05\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    near = LongShot("KXBTC15M-5", "t", "", 0.91, 0.92, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="edge", series="KXBTC15M", side="yes")
    far = LongShot("KXBTC15M-5", "t", "", 0.96, 0.97, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="edge", series="KXBTC15M", side="yes")
    ok = runner.evaluate(s, None, None, store, near, estimators={"market": Fixed()})
    assert ok.action == "bet"  # the model (0.90) is 2 cents under 0.92: inside the 5 cents allowed
    no = runner.evaluate(s, None, None, store, far, estimators={"market": Fixed()})
    assert no.action == "pass" and "under the price" in no.note  # 0.90 against 0.97: 7 cents under


def test_a_two_way_trend_lane_buys_no_in_a_downtrend_and_yes_in_an_uptrend(tmp_path, monkeypatch):
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.two]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\ntrend_down_max=-0.5\ntrend_up_min=0.5\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    now = _time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-4", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    def mk(side):
        return LongShot("KXBTC15M-4", "t", "", 0.55, 0.56, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="two", series="KXBTC15M", side=side)


    def fill(frac_move):
        ticks.history.clear()
        variants._broad_memo = (0, None)
        for ser in variants.BROAD_SERIES:
            index = variants.crypto_index(ser)
            for j in range(1900):
                tt = now - 3800.0 + j * 2.0
                frac = max(0.0, (tt - (now - 3600.0)) / 3600.0)
                ticks.history.note(index, tt, 100.0 * (1.0 + frac_move * frac))

    try:
        fill(-0.012)  # down 1.2% over the hour
        assert runner.evaluate(s, None, None, store, mk("no"), estimators={"market": Fixed()}).action == "bet"
        assert runner.evaluate(s, None, None, store, mk("yes"), estimators={"market": Fixed()}).action == "pass"
        fill(0.012)  # up 1.2%
        assert runner.evaluate(s, None, None, store, mk("yes"), estimators={"market": Fixed()}).action == "bet"
        assert runner.evaluate(s, None, None, store, mk("no"), estimators={"market": Fixed()}).action == "pass"
        fill(0.001)  # flat: neither side
        flat = runner.evaluate(s, None, None, store, mk("yes"), estimators={"market": Fixed()})
        assert flat.action == "pass" and "buys NO while" in flat.note and "YES while" in flat.note
    finally:
        ticks.history.clear()
        variants._broad_memo = (0, None)


def test_wave_features_read_the_swing_range_and_where_the_buy_sits_in_it():
    import math

    ts = [1000.0 + k for k in range(500)]
    vals = [100.0 * math.exp(0.0003 * math.sin(k / 20.0)) for k in range(500)]  # a steady swing, trough at k = 345
    at = ts[345]
    yes = variants.wave_features(ts, vals, "yes", at)
    no = variants.wave_features(ts, vals, "no", at)
    assert yes is not None and no is not None
    assert yes["amp"] > 2.0 and yes["pos"] < 0.1  # YES is at the low of its swing
    assert no["pos"] > 0.9  # the same moment is NO's high
    assert variants.wave_features(ts[:200], vals[:200], "yes", ts[199]) is None  # not five minutes of history
    assert variants.wave_features(ts, [100.0] * 500, "yes", at) is None  # no movement at all


def test_a_lane_with_a_wave_rule_buys_the_low_of_a_swing_and_shows_it(tmp_path, monkeypatch):
    import math
    import time as _time

    from kalshi_moonshot import config, regime, runner, ticks, variants
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.store import Store
    from kalshi_moonshot.strategy import LongShot

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.wavy]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nwave_amp_min=2.0\nwave_pos_max=0.5\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    now = _time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-9", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    index = variants.crypto_index("KXBTC15M")
    yes = LongShot("KXBTC15M-9", "t", "", 0.12, 0.13, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="wavy", series="KXBTC15M", side="yes")
    try:
        for phase, expect in ((-math.pi / 2, "bet"), (math.pi / 2, "pass")):  # trough now, then peak now
            ticks.history.clear()
            for k in range(176):
                t = now - 350.0 + k * 2.0
                ticks.history.note(index, t, 100.0 * math.exp(0.0003 * math.sin((t - now) / 20.0 + phase)))
            d = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
            assert d.action == expect and "[wave amp" in d.note
            if expect == "pass":
                assert "waits for the low" in d.note
        ticks.history.clear()
        d = runner.evaluate(s, None, None, store, yes, estimators={"market": Fixed()})
        assert d.action == "pass" and "unreadable" in d.note  # no history: no buy
    finally:
        ticks.history.clear()
