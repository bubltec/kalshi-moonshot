import math
import sqlite3
from types import SimpleNamespace

import pytest

from kalshi_moonshot import grid
from kalshi_moonshot.store import Store


def ticks(start, series):
    """(ts, yes_bid, yes_ask) one per second from a list of yes bids."""
    return [(start + i, b, round(b + 0.01, 2)) for i, b in enumerate(series)]


def test_a_run_up_is_sold_by_the_peak_crash_exit_and_a_slide_is_held_to_settlement():
    close = 10_000.0
    start = close - 900
    # YES asked 0.40 at minute 4, runs to 0.60 for ten seconds, then falls back and settles NO
    bids = [0.39] * 5 + [0.50] * 5 + [0.60] * 10 + [0.52] * 15 + [0.30] * 30
    rows = ticks(start + 240, bids)
    got = {(mb, pb): roi for mb, pb, roi, _t, _a in grid.replay(rows, close, "yes", "no")}
    assert (1, 3) in got  # minutes 3-6, ask 0.40-0.50
    assert got[(1, 3)] > 0.1  # sold on the way down from the 0.60 peak, still well over cost
    slide = ticks(start + 240, [0.39] * 5 + [0.36] * 5 + [0.30] * 40)
    held = {(mb, pb): roi for mb, pb, roi, _t, _a in grid.replay(slide, close, "yes", "no")}
    assert held[(1, 3)] == pytest.approx(-1.0)  # never armed: held to settlement, which lost


def test_the_no_side_is_replayed_from_the_yes_book():
    close = 10_000.0
    rows = ticks(close - 900 + 100, [0.60] * 30)  # NO asks 1 - 0.60 = 0.40 on the YES bid side
    cells = {(mb, pb) for mb, pb, _r, _t, _a in grid.replay(rows, close, "no", "no")}
    assert (0, 2) in cells or (0, 3) in cells  # 1.00 - 0.60 is about 0.39-0.40


def test_buckets_follow_the_side_bought():
    assert grid.bucket(1.0, "yes") == "with" and grid.bucket(1.0, "no") == "against"
    assert grid.bucket(-1.0, "yes") == "against" and grid.bucket(0.2, "yes") == "flat"


def fill(store, rois, mb=2, pb=4, alpha="with", beta="flat", edge="fair"):
    for i, roi in enumerate(rois):
        store.db.execute(
            "INSERT INTO grid_obs (ticker, series, side, mband, pband, roi, close_ts, alpha, beta, edge) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"T{i}-{mb}{pb}{alpha}{beta}{edge}", "KXBTC15M", "yes", mb, pb, roi, 100 + i, alpha, beta, edge))
    store.db.commit()


def test_pick_needs_enough_replays_and_a_mean_clear_of_zero(tmp_path):
    store = Store(tmp_path / "m.db")
    spec = SimpleNamespace(grid_min_n=30, grid_z=3.0)
    series = ("KXBTC15M",)
    go, why = grid.pick(store, spec, series, 7.0, 0.55, "fair", "with", "flat")
    assert not go and "only 0 replays" in why
    fill(store, [0.5 if i % 2 else -0.3 for i in range(40)])  # mean +0.1, wide spread
    go, why = grid.pick(store, spec, series, 7.0, 0.55, "fair", "with", "flat")
    assert not go and "pass" in why  # 40 replays, but the mean does not clear zero by 3 standard errors
    fill(store, [0.4 + 0.01 * (i % 5) for i in range(60)], alpha="against", beta="flat")  # a steady winner under other conditions
    go, why = grid.pick(store, spec, series, 7.0, 0.55, "fair", "against", "flat")
    assert go and "buy" in why and "price+hour+market" in why
    go, why = grid.pick(store, spec, series, 7.0, 0.55, None, None, None)  # unknown conditions fall back to the pooled cell
    assert not go or "base" in why


def test_the_lane_is_a_grid_lane_and_paper():
    from kalshi_moonshot import config, lanes

    s = config.load()
    spec = lanes.lane_specs(s).get("learned_entry")
    if spec is None:
        return
    assert spec.grid and spec.grid_z >= 3.0 and not s.is_live("learned_entry")


def make_tape(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE feed_ticks (ts REAL NOT NULL, symbol TEXT NOT NULL, px REAL NOT NULL)")
    con.execute("CREATE TABLE index_ticks (ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quarter_avg REAL, src_ms INTEGER)")
    px = 100.0
    for i in range(0, 3600 * 12, 30):  # twelve hours of prices every 30 s, wandering about a percent
        px *= 1.0 + 0.002 * math.sin(i / 400.0) * (1 if (i // 900) % 2 else -1)
        con.execute("INSERT INTO feed_ticks VALUES (?,?,?)", (1.0e9 + i, "hl:xyz:GOLD", px))
    con.commit()
    return con


def test_the_price_against_fair_value_is_cheap_when_the_market_quotes_a_toss_up_far_under_even(tmp_path):
    con = make_tape(tmp_path / "t.db")
    f = grid.Features(con)
    ts = 1.0e9 + 6 * 3600.0
    spot = con.execute("select px from feed_ticks where ts<=? order by ts desc limit 1", (ts,)).fetchone()[0]
    # the price is right at the strike (a toss-up), so YES at 29 cents is cheap and at 70 cents is dear
    assert f.edge("KXGOLD15M", ts, spot, 6.0, 0.29, "yes") == "cheap"
    assert f.edge("KXGOLD15M", ts, spot, 6.0, 0.50, "yes") == "fair"
    assert f.edge("KXGOLD15M", ts, spot, 6.0, 0.70, "yes") == "dear"
    assert f.edge("KXGOLD15M", ts, None, 6.0, 0.29, "yes") is None  # no strike, no opinion


def test_a_backup_is_whole_and_old_ones_are_pruned(tmp_path):
    from kalshi_moonshot import backup

    src = tmp_path / "a.db"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE t (x)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    con.commit()
    dest = tmp_path / "backups" / "a-20261022.db"
    assert backup.copy_db(src, dest)
    assert sqlite3.connect(dest).execute("select count(*) from t").fetchone()[0] == 1000
    assert not backup.copy_db(tmp_path / "missing.db", tmp_path / "backups" / "x.db")
    for d in ("20261020", "20261021", "20261023"):
        (tmp_path / "backups" / f"a-{d}.db").write_bytes(b"x")
    backup.prune(tmp_path / "backups", "a", 2)
    assert sorted(p.name for p in (tmp_path / "backups").glob("a-*.db")) == ["a-20261022.db", "a-20261023.db"]
