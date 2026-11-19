"""The reordered buy as a lane (2026-11-17, user: "the ladder is something that should come LAST in the chain. all it does is evaluate the worth of the bet, not what side to pick"; "the risk score makes way more sense to go FIRST"; "Set up the paper lane"): the risk score says which side (a risk at or under the lane's level that has held), the ladder's worth must clear the lane's edge bar, and the score's own check against the cost is switched off (risk_score_edge = -1) so the worth is judged once, by the ladder. Through a lane file and runner.evaluate; the settings are the ones the paper lane crypto_risk_score carries in data/strategies.toml from 2026-11-17 19:45."""
import time

import pytest

from kalshi_moonshot import config, regime, riskscore, runner, ticks
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

LANE = "[lane.rf]\nestimator='market'\nprice=[0.10,0.85]\nhours=[0.0,1.0]\nsides=['yes','no']\nfixed_stake=1.0\nseries=['KXBTC15M']\nneeds_model_edge=true\nmin_edge_room=0.12\nrisk_score_buy=0.25\n"


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()


class Fixed:
    def __init__(self, p):
        self.p = p

    def estimate(self, client, settings, shot):
        return Estimate(self.p, "fixed")


def decide(tmp_path, monkeypatch, worth_yes, score, ask=0.80):
    """One YES candidate at `ask`: the ladder has YES worth `worth_yes`, the risk score has had it at `score` now, 30 s ago and a minute ago."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE)
    monkeypatch.setattr(riskscore, "live", lambda *a, **k: ([score] * 3, ask - 0.005, [ask - 0.005] * 3))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", round(ask - 0.01, 2), ask, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="rf", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed(worth_yes)})


def test_integration_a_side_the_score_favours_and_the_ladder_rates_worth_more_is_bought(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, worth_yes=0.92, score=0.80)  # risk 20%, held; worth 0.92 for 0.80
    assert out.action == "bet", out.note


def test_integration_the_score_alone_does_not_buy_the_ladder_has_to_rate_the_worth(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, worth_yes=0.81, score=0.80)  # the score favours it; the ladder has it worth what it costs
    assert out.action == "pass" and "edge screen" in out.note

