"""The shape_timed step selling (lane field curve_risk_shape_live; pathshape.py, watcher._shape_timed_read).

2026-11-19, user: "crypto_risk_score is already paper. just turn it on"; "theres factors to consider when flat". A bet that has been behind for a tenth of the time left is sold when its path over that stretch is trend_against (whatever the risk score says), or flat and the score's chance does not clear what the sale pays net of its fee (no cushion under it). A flat bet whose chance is over that is held; a bet that is ahead, or has not been behind long enough, is not touched. Off (the field's default) the step only records a row, as before."""
import pytest

from kalshi_moonshot import watcher
from tests.test_risk_exit_flow import held
from tests.test_shape_shadow import FALL, _clean, rows, run, seed  # noqa: F401

LIVE = "sell_chain=['shape_timed']\ncurve_risk_shape_live=true\ncurve_risk_score=true\n"


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


def test_integration_a_flat_bet_with_no_cushion_is_sold_and_with_cushion_is_held(tmp_path, monkeypatch):
    seed(0.002)
    _score(monkeypatch, 0.40)  # the sale pays about 0.58 net: the score's chance is under it, no cushion
    w = held(tmp_path, monkeypatch, live=True, extra=LIVE)
    sold = run(w, [0.60] * 120)
    assert len(sold) == 1 and "flat" in sold[0].why and "no cushion" in sold[0].why
    assert rows(tmp_path, kind="shape_timed")[-1]["sold"] is True and rows(tmp_path, kind="shape_timed")[-1]["cushion"] < 0
    seed(0.002)
    _score(monkeypatch, 0.80)  # the score says 0.80 against a sale of about 0.58: a cushion, so the flat bet is held
    w = held(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, monkeypatch, live=True, extra=LIVE)
    assert run(w, [0.60] * 120) == []
    r = rows(tmp_path / "b", kind="shape_timed")
    assert len(r) == 1 and r[0]["sold"] is False and r[0]["would_sell"] is True and r[0]["cushion"] > 0  # one shadow row, no sale, and no repeat of the row


def test_integration_without_the_field_the_step_only_records(tmp_path, monkeypatch):
    seed(0.002)
    _score(monkeypatch, 0.40)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape_timed']\ncurve_risk_score=true\n")
    assert run(w, FALL) == []
    got = rows(tmp_path, kind="shape_timed")
    assert len(got) == 1 and got[0]["live"] is False and got[0]["sold"] is False


def test_integration_a_bet_that_is_ahead_is_never_sold_by_it(tmp_path, monkeypatch):
    seed(0.002)
    _score(monkeypatch, 0.40)
    w = held(tmp_path, monkeypatch, live=True, extra=LIVE)
    assert run(w, [0.90] * 120) == [] and rows(tmp_path, kind="shape_timed") == []  # bought at 0.81, bid 0.90


def test_integration_a_lane_without_the_score_holds_a_flat_bet(tmp_path, monkeypatch):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape_timed']\ncurve_risk_shape_live=true\n")
    assert run(w, [0.60] * 120) == []  # the chance is the net bid itself: cushion zero, nothing to say the bet is worth less than the sale pays
    assert rows(tmp_path, kind="shape_timed")[-1]["sold"] is False
