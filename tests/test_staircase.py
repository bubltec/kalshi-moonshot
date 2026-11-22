"""The staircase sell step (watcher._sell_staircase, priskp.Staircase): the bid's swings every 10 seconds, a sale once the bid falls under the last confirmed swing low after the bet has been ahead."""
import json

import pytest

from kalshi_moonshot import ticks as ticks_mod
from tests.test_risk_exit_flow import held, tick


@pytest.fixture(autouse=True)
def _clean():
    ticks_mod.mids._rows.clear()
    yield
    ticks_mod.mids._rows.clear()


def lane(tmp_path, monkeypatch, live=True):
    return held(tmp_path, monkeypatch, live=live, take="true", extra="sell_chain=['staircase']")


def walk(w, bids, left0=430):
    """One quote every 10 s; returns the sale signals raised, with the left at which each came."""
    out = []
    for k, b in enumerate(bids):
        got = tick(w, b, left0 - 10 * k)
        if got:
            out.append((k, b, got[0].why))
            break
    return out


def swings():
    up = [0.82 + 0.01 * i for i in range(9)]  # 0.82 to 0.90, the bet bought at 0.81 is ahead from the start
    down = [0.90 - 0.01 * i for i in range(1, 13)]  # a pullback to 0.78: confirms the swing high, then the lowest point
    bounce = [0.78 + 0.01 * i for i in range(1, 10)]  # a bounce off 0.78 of more than a swing: confirms the swing low
    return up, down, bounce


def test_integration_it_sells_when_the_bid_falls_under_the_last_swing_low_after_being_ahead(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch)
    up, down, bounce = swings()
    assert walk(w, up + down + bounce) == []  # no sale while the swing low is still to be confirmed or held
    got = walk(w, [0.79, 0.78, 0.77], left0=430 - 10 * 30)
    assert got and "staircase" in got[0][2] and "swing low 0.78" in got[0][2] and "sold at staircase" in got[0][2]
    assert got[0][1] == 0.77


def test_integration_a_bet_that_was_never_ahead_is_not_sold_by_the_staircase(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch)
    fall = [0.80 - 0.01 * i for i in range(10)]
    rise = [0.71 + 0.01 * i for i in range(10)]
    back = [0.80 - 0.01 * i for i in range(14)]  # under the swing low, still never in profit after both fees on a 0.81 buy
    assert walk(w, fall + rise + back) == []


def test_integration_shadow_it_logs_one_would_sell_and_sells_nothing(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch, live=False)
    up, down, bounce = swings()
    assert walk(w, up + down + bounce + [0.79, 0.78, 0.77, 0.76], left0=430) == []
    f = tmp_path / "risk_shadow.jsonl"
    rows = [x for x in map(json.loads, f.read_text().splitlines()) if x.get("kind") == "staircase"] if f.exists() else []
    assert len(rows) == 1  # said once; nothing was sold


def test_integration_nothing_is_sold_in_the_last_minute(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch)
    up, down, bounce = swings()
    assert walk(w, up + down + bounce, left0=430) == []
    assert walk(w, [0.77, 0.70, 0.60], left0=55) == []
