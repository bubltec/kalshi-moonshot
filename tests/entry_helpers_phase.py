"""The entry-phase gate: skip a buy at the top of a wave (2026-11-15 night, user: "betting when we shouldn't, and on the wrong side ... I would like to see if we can do that too")."""
import math

import pytest

from kalshi_moonshot import config, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


def path(now, run_pct=0.0, last5_pct=0.0, noise_pct=0.01, minutes=90):
    """A perp in minute ticks for `minutes` minutes ending at `now`: a +-noise_pct zig-zag, a steady `run_pct` percent climb over the 13 minutes before the last 5 and `last5_pct` over the last 5 minutes (negative = falling)."""
    out = []
    lvl = 0.0
    for k in range(minutes, -1, -1):  # k minutes ago
        step = noise_pct * (1 if k % 2 == 0 else -1)
        if 5 < k <= 18:
            step += run_pct / 13.0
        elif 0 < k <= 5:
            step += last5_pct / 5.0
        lvl += step
        out.append((now - 60.0 * k, 100.0 * math.exp(lvl / 100.0)))
    return out


def seed(now, **kw):
    ticks.history.clear()
    for ser in variants.BROAD_SERIES:
        for ts, v in path(now, **kw):
            ticks.history.note(variants.crypto_index(ser), ts, v)
    from kalshi_moonshot.watcher import perp_index

    for ts, v in path(now, **kw):
        ticks.history.note(perp_index("KXBTC15M"), ts, v)


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._broad_memo = (0, None)


LANE = "[lane.ph]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def decide(tmp_path, monkeypatch, now, extra, side="yes"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ph", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})

