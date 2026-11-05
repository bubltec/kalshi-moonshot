import pytest

from kalshi_moonshot import scorecard
from kalshi_moonshot.scorecard import Claim


def row(lane, ts, action="bet", mode="trade", side="yes", ask=0.85, stake=10.0, exit_price=None, exit_reason=None, reason="", ticker="KX-1", result="yes"):
    return {"lane": lane, "ts": ts, "action": action, "mode": mode, "side": side, "ask": ask, "stake_usd": stake, "exit_price": exit_price, "exit_reason": exit_reason, "reason": reason, "ticker": ticker, "result": result}


def test_a_lane_is_scored_on_the_bets_after_its_adoption_and_judged_against_its_claim():
    claim = Claim("lane", "lane_roi", ("L",), 1000.0, 0.10, "text", "src", min_n=3)
    rows = [row("L", 500.0, result="no"), row("L", 1100.0), row("L", 1200.0), row("L", 1300.0, result="no", ticker="KX-2"), row("OTHER", 1100.0)]  # the first bet is from before adoption
    got = scorecard.evaluate(claim, rows)
    assert got["n"] == 3 and got["wins"] == 2 and got["staked"] == pytest.approx(30.0)
    assert got["net"] == pytest.approx(2 * scorecard.bet_pnl(0.85, 10.0, True, None) - 10.0)
    assert scorecard.verdict(claim, got) == "not holding"  # the two wins pay about $1.5 each: 3.0 - 10 is a loss while the claim was a gain
    assert scorecard.verdict(claim, {**got, "n": 2}) == "too early"


def test_a_rule_that_refuses_bets_is_scored_on_what_the_refused_would_have_done_once_each():
    claim = Claim("gate", "refused_ev10", ("L",), 1000.0, -0.40, "text", "src", min_n=2, reason_key="outside the lane", reason_exclude="choppy")
    rows = [row("L", 1100.0, action="pass", mode="observe", reason="volatility regime 1.5x its normal is outside the lane's 0-1.3", result="no", ticker="A"),
            row("L", 1101.0, action="pass", mode="observe", reason="volatility regime 1.5x its normal is outside the lane's 0-1.3", result="no", ticker="A"),  # the same market again: counted once
            row("L", 1102.0, action="pass", mode="observe", reason="volatility regime 1.6x its normal is outside the lane's 0-1.3", result="yes", ticker="B"),
            row("L", 1103.0, action="pass", mode="observe", reason="a different refusal", result="no", ticker="C")]
    got = scorecard.evaluate(claim, rows)
    assert got["n"] == 2 and got["wins"] == 1
    assert got["live"] == pytest.approx((scorecard.ticket_pnl(0.85, False) + scorecard.ticket_pnl(0.85, True)) / 2)
    assert got["live"] < -3 and scorecard.verdict(claim, got) == "holding"  # one loss and one small win: the refused set loses, as claimed
    winners = scorecard.evaluate(claim, [row("L", 1100.0 + k, action="pass", mode="observe", reason="is outside the lane", result="yes", ticker=f"W{k}") for k in range(3)])
    assert winners["live"] > 0 and scorecard.verdict(claim, winners) == "not holding"  # refusing them cost money


def test_an_exit_is_scored_by_what_it_made_against_holding():
    claim = Claim("exit", "exit_vs_hold", ("L",), 1000.0, 0.5, "text", "src", min_n=2, exit_key="shock exit")
    sold_a_loser = row("L", 1100.0, exit_price=0.40, exit_reason="shock exit: spot moved", result="no", ticker="A")  # sold at 0.40, held to zero: saved money
    sold_a_winner = row("L", 1200.0, exit_price=0.60, exit_reason="shock exit: spot moved", result="yes", ticker="B")  # sold a winner: cost money
    held = row("L", 1300.0, ticker="C")
    got = scorecard.evaluate(claim, [sold_a_loser, sold_a_winner, held])
    assert got["n"] == 2 and got["wins"] == 1
    expect = (scorecard.bet_pnl(0.85, 10.0, False, 0.40) - scorecard.bet_pnl(0.85, 10.0, False, None)) + (scorecard.bet_pnl(0.85, 10.0, True, 0.60) - scorecard.bet_pnl(0.85, 10.0, True, None))
    assert got["live"] == pytest.approx(expect / 2)


def test_the_registry_is_consistent_and_the_report_runs(tmp_path, monkeypatch):
    from kalshi_moonshot import config
    from kalshi_moonshot.store import Store

    ids = [c.id for c in scorecard.CLAIMS]
    assert len(ids) == len(set(ids)) and all(c.kind in ("lane_roi", "refused_ev10", "exit_vs_hold") for c in scorecard.CLAIMS)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    Store(s.db_path)
    lines = scorecard.report(s, 14.0, now=2_000_000_000.0)
    assert lines[0].startswith("Does each lane") and any("wild-fade" in x for x in lines) and any("too early" in x for x in lines)
