"""The risk exit's line sale has its own switch (lane field curve_risk_line_live): 2026-11-17, user, on KXBTC15M 18:45 (NO bought at 0.59, sold by the line at 0.05 with 273 s left, back at 0.62 three minutes later and settled a winner): "it sold it way too early, cause it came back down"; "disable the 12c sale". Through the real watcher path: lane file -> stored bet -> refresh() -> quote events -> sale signal."""
import json

from kalshi_moonshot import config, lanes
from tests.test_risk_exit_flow import LANE, PATH, climb, held, level, signals, sold_after, yes_bid

OFF = "curve_risk_line_live=false"


def test_integration_with_the_switch_on_the_line_sells_a_bet_far_under_it(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0)  # no max loss: only the line can sell a loss
    took = sold_after(w, 0.20, 450)
    assert took is not None


def test_integration_with_the_switch_off_the_same_bet_is_not_sold(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra=OFF)
    assert w.targets[next(iter(w.targets))].risk_line_live is False
    assert sold_after(w, 0.20, 450) is None  # 200 s at a bid of 0.20 under a line of 0.5: held


def test_integration_with_the_switch_off_the_would_sell_is_logged_and_recorded_once(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra=OFF)
    sold_after(w, 0.20, 450)
    rows = [json.loads(x) for x in (tmp_path / "risk_shadow.jsonl").read_text().splitlines()]
    sales = [r for r in rows if not r.get("live") and not r.get("held")]
    assert len(sales) == 1, rows  # one would-sell for the bet, so the switch can be scored against what the bet got


def test_integration_with_the_switch_off_the_profit_take_still_sells(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", max_loss=0, extra=OFF)
    assert climb(w, 0.82, 0.92, 700) is None
    sig = level(w, 0.92, 700)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_with_the_switch_off_the_max_loss_still_sells(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, extra=OFF)  # the lane file's max loss of 0.5 is on
    sold = None
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
        out = signals(w)
        if out:
            sold = out[0].why
            break
    assert sold is not None and sold.startswith("curve risk exit (max loss)")


def test_the_lane_version_changes_only_when_the_line_sale_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def version(extra):
        (tmp_path / "strategies.toml").write_text(LANE.format(live="true", confirm=-1, hold=0, take="false", share=0.015, cushion="false", stretch=0, squeeze=0, max_loss=0) + extra + "\n")
        s = config.load()
        return lanes.settings_of(lanes.spec_for(s, "risk_only"), s)

    assert version("curve_risk_line_live=true") == version("")
    assert version(OFF) != version("")
