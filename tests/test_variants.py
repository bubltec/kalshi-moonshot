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


def _cell(n, net):
    return {"n": n, "net": net}


def test_tune_skips_only_cells_that_lost_in_both_halves(monkeypatch):
    older = {("crypto", "BTC", "yes", "F"): _cell(60, -60.0), ("crypto", "ETH", "yes", "F"): _cell(60, -60.0), ("crypto", "BTC", "no", "G"): _cell(100, -70.0),
             ("crypto", "BTC", "yes", "E"): _cell(100, +8.0), ("crypto", "BTC", "no", "A"): _cell(100, -3.0), ("crypto", "BTC", "no", "B"): _cell(10, -50.0)}
    newer = {("crypto", "BTC", "yes", "F"): _cell(60, -70.0), ("crypto", "ETH", "yes", "F"): _cell(60, -50.0), ("crypto", "BTC", "no", "G"): _cell(100, +46.0),
             ("crypto", "BTC", "yes", "E"): _cell(100, -112.0), ("crypto", "BTC", "no", "A"): _cell(100, -2.0), ("crypto", "BTC", "no", "B"): _cell(10, -50.0)}
    boards = iter([older, newer])
    monkeypatch.setattr(variants, "market_scoreboard", lambda *a, **k: next(boards))
    out = variants.tune(None, days=10.0, min_n=40, min_loss=0.10, now=1_000_000.0)
    assert out["skip"] == {"crypto": ["F-yes"], "commodity": []}  # F-yes lost both halves; G won one, E won one, A lost a cent, B had too few bets


def test_a_lane_reads_the_tuned_list_for_its_own_asset_class_and_ignores_a_stale_one(tmp_path, monkeypatch):
    from types import SimpleNamespace

    s = SimpleNamespace(data_dir=tmp_path)
    variants._tuned_cache.clear()
    monkeypatch.setattr(variants.time, "time", lambda: 2_000_000.0)
    (tmp_path / variants.TUNED_FILE).write_text('{"at": 1999000.0, "skip": {"crypto": ["F-yes"], "commodity": ["G"]}}')
    assert variants.tuned_skips(s, "KXBTC15M") == ("F-yes",)
    assert variants.tuned_skips(s, "KXWTI15M") == ("G",)
    assert variants.tuned_skips(s, "KXNOTHING") == ()
    (tmp_path / variants.TUNED_FILE).write_text('{"at": 1000.0, "skip": {"crypto": ["F-yes"]}}')  # older than three days
    variants._tuned_cache.clear()
    assert variants.tuned_skips(s, "KXBTC15M") == ()
    (tmp_path / variants.TUNED_FILE).write_text("not json")
    variants._tuned_cache.clear()
    assert variants.tuned_skips(s, "KXBTC15M") == ()


def test_a_crypto_variety_is_read_against_the_strike(monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE index_ticks (index_id TEXT, ts REAL, value REAL)")
    index = variants.crypto_index("KXBTC15M")
    now = 5000.0
    conn.executemany("INSERT INTO index_ticks VALUES (?,?,?)", [(index, now - 340 + k, 100.2 + 0.001 * (k % 2)) for k in range(341)])  # 20 bp over a strike of 100, steady
    monkeypatch.setattr(variants.db, "connect", lambda *a, **k: conn)
    s = SimpleNamespace(tape_path="x")
    assert variants.variety_now(s, "KXBTC15M", "yes", 300.0, now=now, strike=100.0) == "B"
    assert variants.variety_now(s, "KXBTC15M", "yes", 300.0, now=now) is None  # no strike, no variety
    assert variants.variety_now(s, "KXBTC15M", "yes", 300.0, now=now, strike=0.0) is None
