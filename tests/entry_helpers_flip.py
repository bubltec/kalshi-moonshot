"""The flip rule (lane field flip_skip_minutes): 2026-11-16, user, on DOGE 09:30 YES bought at the top of an 80-second spike and stopped out three minutes later: "It bought the wrong side tho ... the history shows a likely outcome"; studies/results/2026-11-16-spike-side.txt."""

import pytest

from kalshi_moonshot import config, regime, runner, ticks
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

LANE = "[lane.fl]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def spike(now, opened, base=0.40, top=0.62, last=60):
    """(time, YES mid) rows, one a second from the open: YES sits at `base` all market, then jumps to `top` over the last `last` seconds."""
    n = int(now - opened)
    return [(opened + k, base + (0.004 if k % 2 else -0.004) if k <= n - last else base + (top - base) * (k - (n - last)) / last) for k in range(n + 1)]


def seed(rows, ticker="KXBTC15M-7"):
    ticks.mids.clear()
    for ts, mid in rows:
        ticks.mids.note(ticker, ts, mid - 0.005, mid + 0.005)


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()


def decide(tmp_path, monkeypatch, now, extra, side="yes", left=600.0):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + left, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.61, 0.62, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="fl", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


# The trailing-side buy (2026-11-16, user: "ok sounds good what you suggested"): lane fields entry_led_max and wave_pos_min. studies/results/2026-11-16-buy-state-table.txt.
def comeback(now, opened, base=0.25, low=0.15, top=0.45, pull=None):
    """(time, YES mid) rows: YES trails at `base`, dips to `low` mid-market, then climbs to `top`; `pull` sets where it ends (None = at `top`)."""
    n = int(now - opened)
    end = top if pull is None else pull
    rows = []
    for k in range(n + 1):
        f = k / n
        mid = base if f < 0.3 else low if f < 0.5 else low + (top - low) * (f - 0.5) / 0.4 if f < 0.9 else top + (end - top) * (f - 0.9) / 0.1
        rows.append((opened + k, mid + (0.002 if k % 2 else -0.002)))
    return rows


TRAIL = "entry_led_max=0.2\nwave_quote=true\nwave_pos_min=0.2\nwave_pos_max=0.8"

