import json
import math

import pytest

from kalshi_moonshot import db, knn
from kalshi_moonshot.store import Store


def test_an_entry_becomes_a_point_scaled_so_no_coordinate_dominates():
    v = knn.vector(minute=7.5, ask=0.40, spread=0.02, d90=-0.04, d240=-0.08, alpha=1.0, beta=-0.5, gap=0.10)
    assert len(v) == knn.DIM
    assert all(abs(x) <= 3.0 for x in v)
    unknown = knn.vector(minute=7.5, ask=0.40, spread=0.02, d90=None, d240=None, alpha=None, beta=None, gap=None)
    assert unknown[5] == unknown[6] == unknown[7] == 0.0 and unknown[8] == 0.0  # missing price feeds are zeros, flagged as unknown
    assert v[8] > 0
    far = knn.vector(minute=7.5, ask=0.90, spread=0.02, d90=-0.04, d240=-0.08, alpha=1.0, beta=-0.5, gap=0.10)
    assert math.dist(v, far) > math.dist(v, knn.vector(7.5, 0.41, 0.02, -0.04, -0.08, 1.0, -0.5, 0.10))  # a bigger price gap is a bigger distance


def test_the_estimate_counts_windows_not_entries_and_weights_by_closeness():
    # one window with 40 near-identical entries that all won must not outweigh ten other windows that lost
    near = [("W1", "yes", 1.0, 0.1) for _ in range(40)] + [(f"L{i}", "yes", -1.0, 0.1) for i in range(10)]
    est = knn.estimate(near)
    assert est["groups"] == 11
    assert est["mean"] < 0  # by windows: 1 win, 10 losses, not 40 against 10
    closer = [("A", "yes", 1.0, 0.01), ("B", "yes", 1.0, 0.01), ("C", "yes", 1.0, 0.02), ("D", "yes", 1.0, 0.02), ("E", "yes", 1.0, 0.03),
              ("F", "yes", -1.0, 1.0), ("G", "yes", -1.0, 1.0), ("H", "yes", -1.0, 1.0), ("I", "yes", -1.0, 1.0), ("J", "yes", -1.0, 1.0)]
    assert knn.estimate(closer)["mean"] > 0  # the near ones count more than the far ones
    assert knn.estimate([("X", "yes", 0.3, 0.1)])["n_eff"] == 0.0  # too few to say anything


def test_it_buys_only_when_the_cautious_end_clears_zero_with_enough_windows():
    strong = {"mean": 0.30, "se": 0.05, "n_eff": 60.0, "groups": 60.0, "bandwidth": 0.2}
    weak = {"mean": 0.05, "se": 0.05, "n_eff": 60.0, "groups": 60.0, "bandwidth": 0.2}
    few = {"mean": 0.60, "se": 0.05, "n_eff": 8.0, "groups": 8.0, "bandwidth": 0.2}
    assert knn.decide(strong, 25, 3.0)[0] is True
    assert knn.decide(weak, 25, 3.0)[0] is False and "pass" in knn.decide(weak, 25, 3.0)[1]
    assert knn.decide(few, 25, 3.0)[0] is False and "only 8" in knn.decide(few, 25, 3.0)[1]


def _put(store, ticker, side, roi, vec, rule="trail", series="KXBTC15M", close=2000):
    feats = knn._lit(vec) if db.is_pg(store.db) else json.dumps(vec)
    store.db.execute("INSERT INTO entry_vec (ticker, series, side, close_ts, rule, minute, ask, roi, feats) VALUES (?,?,?,?,?,?,?,?,?)",
                     (ticker, series, side, close, rule, 5.0, 0.5, roi, feats))
    store.db.commit()


def test_the_search_finds_the_nearest_stored_entries_and_respects_rule_series_and_time(tmp_path):
    store = Store(tmp_path / "m.db")
    if not knn.ensure_schema(store):
        pytest.skip("pgvector is not installed in this Postgres")
    here = knn.vector(5.0, 0.40, 0.02, 0.0, 0.0, 0.0, 0.0, 0.0)
    for i in range(8):
        _put(store, f"NEAR-{i}", "yes", 0.5, [v + 0.001 * i for v in here])
    for i in range(8):
        _put(store, f"FAR-{i}", "yes", -1.0, knn.vector(12.0, 0.90, 0.02, 0.3, 0.3, 1.0, 1.0, 0.4))
    _put(store, "OTHERRULE", "yes", 9.0, here, rule="hold")
    _put(store, "OTHERSERIES", "yes", 9.0, here, series="KXGOLD15M")
    _put(store, "OLD", "yes", 9.0, here, close=10)
    near = knn.neighbors(store, here, ("KXBTC15M",), "trail", since=1000.0, k=8)
    assert [r[0] for r in near][:8] == [f"NEAR-{i}" for i in range(8)]
    assert all(r[2] == 0.5 for r in near)
    est = knn.estimate(knn.neighbors(store, here, ("KXBTC15M",), "trail", since=1000.0, k=16))
    assert est["groups"] == 16 and -1.0 < est["mean"] < 0.5  # the far, losing entries enter once k reaches past the near ones


def test_the_model_is_off_not_wrong_when_there_is_no_history_yet(tmp_path):
    store = Store(tmp_path / "m.db")
    if not knn.ensure_schema(store):
        pytest.skip("pgvector is not installed in this Postgres")
    near = knn.neighbors(store, knn.vector(5, 0.4, 0.02, 0, 0, 0, 0, 0), ("KXBTC15M",), "trail", k=50)
    assert near == [] and knn.decide(knn.estimate(near), 25, 3.0)[0] is False
