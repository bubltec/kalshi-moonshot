"""The shape_timed step selling (pathshape.py, watcher._shape_timed_read).

2026-11-19, user: "crypto_risk_score is already paper. just turn it on"; "theres factors to consider when flat". A bet that has been behind for a tenth of the time left is sold when its path over that stretch is trend_against, whatever the risk score says. A flat path is recorded and held, with or without a cushion (15 of 20 flat sales since 12:01 won at settlement). A bet that is ahead, or has not been behind long enough, is not touched."""
import pytest

from kalshi_moonshot import watcher
from tests.test_risk_exit_flow import held
from tests.test_shape_shadow import FALL, _clean, rows, run, seed  # noqa: F401

LIVE = "sell_chain=['shape_timed']\ncurve_risk_score=true\n"


def _score(monkeypatch, value):
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_score", lambda self, t, now: value)


def test_integration_a_falling_bet_is_sold_after_the_wait_whatever_the_score_says(tmp_path, monkeypatch):
    seed(0.002)
    _score(monkeypatch, 0.95)  # the score says the bet is worth far more than the sale pays: a falling bet is still sold
    w = held(tmp_path, monkeypatch, live=True, extra=LIVE)
    assert run(w, FALL[:30]) == []  # 30 s behind: under the 45 s a market with 449 s left asks
    sold = run(w, FALL[30:], left=449 - 30)
    assert len(sold) == 1 and sold[0].why.startswith("curve risk exit (shape)") and "trend_against" in sold[0].why
    got = rows(tmp_path, kind="shape_timed")
    assert got[-1]["live"] is True and got[-1]["sold"] is True and got[-1]["buy"]["shape"] == "trend_against" and got[-1]["cushion"] > 0


def test_integration_a_flat_bet_is_recorded_and_held_whatever_the_cushion(tmp_path, monkeypatch):
    seed(0.002)
    _score(monkeypatch, 0.40)  # the sale pays about 0.58 net: no cushion, and still a flat bet is not sold
    w = held(tmp_path, monkeypatch, live=True, extra=LIVE)
    assert run(w, [0.60] * 120) == []
    r = rows(tmp_path, kind="shape_timed")
    assert len(r) == 1 and r[0]["sold"] is False and r[0]["would_sell"] is True and r[0]["cushion"] < 0  # one row, no sale, no repeat


def test_integration_a_bet_that_is_ahead_is_never_sold_by_it(tmp_path, monkeypatch):
    seed(0.002)
    _score(monkeypatch, 0.40)
    w = held(tmp_path, monkeypatch, live=True, extra=LIVE)
    assert run(w, [0.90] * 120) == [] and rows(tmp_path, kind="shape_timed") == []  # bought at 0.81, bid 0.90


def test_integration_a_lane_without_the_score_holds_a_flat_bet(tmp_path, monkeypatch):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape_timed']\n")
    assert run(w, [0.60] * 120) == []  # the chance is the net bid itself: cushion zero, nothing to say the bet is worth less than the sale pays
    assert rows(tmp_path, kind="shape_timed")[-1]["sold"] is False
