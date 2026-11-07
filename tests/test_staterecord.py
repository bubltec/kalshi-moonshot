import json
import time

from kalshi_moonshot import config, regime, runner, staterecord, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def test_a_reading_counts_once_per_market_side_state_and_band_and_the_edge_is_shrunk():
    cells: dict = {}
    seen: set = set()
    for _ in range(3):  # the same market side and state read at three moments is one reading
        staterecord.add_reading(cells, seen, "M1", "yes", "DF", 0.40, 0.50)
    staterecord.add_reading(cells, seen, "M2", "yes", "DF", 0.41, -1.0)
    staterecord.add_reading(cells, seen, "M2", "yes", "DF", 0.80, 0.1)  # another price band is its own cell
    assert cells["DF|1"] == [2, -0.5] and cells["DF|2"] == [1, 0.1]
    n, net = staterecord.edge({"cells": {"DF|1": [30, 9.0]}}, "DF", 0.45)
    assert n == 30 and abs(net - 9.0 / 40.0) < 1e-9
    assert staterecord.edge({"cells": {}}, "DF", 0.45) == (0, 0.0)


def test_state_key_needs_every_scale():
    assert staterecord.state_key({"5m": "D", "60m": "F"}) == "DF"
    assert staterecord.state_key({"5m": "D"}) is None


def test_a_record_buy_lane_buys_only_a_state_whose_record_clears_its_bar(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.rec]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nrecord_buy=true\nrecord_min=0.2\nrecord_min_n=15\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    staterecord._cache.update(mtime=None, data=None)
    now = time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    index = variants.crypto_index("KXBTC15M")
    for k in range(1950):  # a steady price 20 bp above the strike: both scales read B
        ticks.history.note(index, now - 3900.0 + k * 2.0, 100.20 + 0.0004 * (k % 2))
    shot = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="rec", series="KXBTC15M", side="yes")
    try:
        none = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert none.action == "pass" and "no record built yet" in none.note
        (tmp_path / "state_record.json").write_text(json.dumps({"built_at": now, "cells": {"BB|2": [30, 9.0]}}))  # 9.0 / 40 = 0.225 per dollar
        staterecord._cache.update(mtime=None, data=None)
        ok = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert ok.action == "bet" and "[state BB, record +0.23 per $1 over 30 markets]" in ok.note
        (tmp_path / "state_record.json").write_text(json.dumps({"built_at": now, "cells": {"BB|2": [30, 6.0], "DF|2": [100, 90.0]}}))  # 0.15: under the bar
        staterecord._cache.update(mtime=None, data=None)
        low = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert low.action == "pass" and "under the lane's" in low.note
        (tmp_path / "state_record.json").write_text(json.dumps({"built_at": now, "cells": {"BB|2": [5, 5.0]}}))  # too few markets
        staterecord._cache.update(mtime=None, data=None)
        few = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert few.action == "pass"
    finally:
        ticks.history.clear()
        staterecord._cache.update(mtime=None, data=None)
