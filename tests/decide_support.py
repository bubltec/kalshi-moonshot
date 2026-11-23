"""Helpers for the tests that run one evaluation through the runner (kept from the old risk-score tests when they were archived, 2026-11-22): a fixed estimator, a settlement-index curve, a market-mid curve and `decide`."""
import math

from kalshi_moonshot import config, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

LANE = "[lane.rs]\nestimator='market'\nprice=[0.05,0.97]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def index(opened, now, start=100.0, lift=0.0, step=5):
    """(times, values): the index wiggles around `start` for 70 minutes before the open, then sits `lift` (a fraction) over it for the market so far."""
    ts, vs = [], []
    t = opened - 4200.0
    k = 0
    while t <= now:
        base = start * (1.0 + (lift if t > opened + 30.0 else 0.0))
        ts.append(t)
        vs.append(base * (1.0 + 0.0004 * math.sin(k / 7.0)))
        t += step
        k += 1
    return ts, vs


def mids(opened, now, level=0.80, flip_at=None):
    """(time, YES mid) rows one a second: at `level` with a small wobble; before `flip_at` seconds in (if given) at 1 - level."""
    return [(opened + k, (1.0 - level if flip_at and k < flip_at else level) + (0.004 if k % 2 else -0.004)) for k in range(int(now - opened) + 1)]


def decide(tmp_path, monkeypatch, now, extra, level=0.80, lift=0.004, flip_at=None, ask=0.81, seed=True):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    left = 420.0
    opened = now + left - 900.0
    ticks.mids.clear()
    ticks.history.clear()
    if seed:
        for ts, v in zip(*index(opened, now, lift=lift), strict=False):
            ticks.history.note(variants.crypto_index("KXBTC15M"), ts, v)
        for ts, mid in mids(opened, now, level=level, flip_at=flip_at):
            ticks.mids.note("KXBTC15M-7", ts, mid - 0.005, mid + 0.005)
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + left, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", ask - 0.01, ask, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="rs", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
