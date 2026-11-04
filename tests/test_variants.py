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
    led.add([_rec(now - 3600 + k, "G", "no", -3.0) for k in range(6)])  # bad recent, and nothing older: judged on the recent stretch alone
    led.add([_rec(now - 3600 + k, "E", "no", -0.2) for k in range(6)] + [_rec(now - 20 * 3600 + k, "E", "no", +2.0, ticker=f"o{k}") for k in range(30)])  # a small dip against a good record
    assert led.skips("crypto", now, hours=6.0, prior_weight=15.0, min_loss=0.10, min_n=5) == ("G-no", "F-yes")  # sorted by (class, side, variety)
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
        '[[variety]]\nletter="H"\nname="near and speeding up"\nwhen="z < 1 and vol_ratio > 1.5"\n'
        '[[variety]]\nletter="J"\nname="bad"\nwhen="__import__(1)"\n'
        '[[variety]]\nletter="B"\nname="reuses a built-in letter"\nwhen="z > 0"\n')
    variants._custom_mtime.clear()
    variants.load_custom(s)
    try:
        assert variants.NAMES["H"] == "near and speeding up" and "J" not in variants.NAMES and variants.NAMES["B"] == "far, flat"
        assert variants.classify_features({"z": 0.4, "slope": 0.0, "crosses": 0, "vol_ratio": 2.0, "since_cross": 300.0}) == "H"
        assert variants.classify_features({"z": 0.4, "slope": 0.0, "crosses": 0, "vol_ratio": 1.0, "since_cross": 300.0}) == "E"  # no custom test holds: the built-in letter
        assert variants.skipped(("H-yes",), "H", "yes") and not variants.skipped(("H-yes",), "H", "no")
    finally:
        (tmp_path / variants.CUSTOM_FILE).unlink()
        variants._custom_mtime.clear()
        variants.load_custom(s)
    assert "H" not in variants.NAMES


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
