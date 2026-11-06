import json
from types import SimpleNamespace

from kalshi_moonshot import marketreview


def row(ts=0, ticker="KXBTC15M-26NOV061200-00", side="yes", ask=0.5, stake=1.0, outcome="yes", exit_price=None, contracts=None):
    return {"ts": ts, "ticker": ticker, "side": side, "ask": ask, "contracts": contracts, "stake_usd": stake, "outcome": outcome, "exit_price": exit_price}


def test_a_win_pays_the_contracts_and_a_loss_costs_the_stake():
    assert marketreview.bet_return(row(ask=0.5, stake=1.0, outcome="yes")) == (1.0, 1.0)
    assert marketreview.bet_return(row(ask=0.5, stake=1.0, outcome="no")) == (1.0, -1.0)


def test_an_exit_sells_at_its_price_and_an_open_bet_has_no_return():
    assert marketreview.bet_return(row(ask=0.5, stake=1.0, outcome="no", exit_price=0.2)) == (1.0, 2 * 0.2 - 1.0)
    assert marketreview.bet_return(row(outcome=None)) is None


def test_too_few_bets_keep():
    got = marketreview.propose([-1.0] * (marketreview.MIN_BETS - 1))
    assert got["action"] == "keep" and got["t"] is None


def test_a_clearly_losing_market_stands_aside_and_a_winning_one_presses():
    losing = [-1.0, -1.0, 0.1] * 6
    assert marketreview.propose(losing)["action"] == "stand_aside"
    winning = [0.1, 0.12, -0.05] * 6
    assert marketreview.propose(winning)["action"] == "press"
    flat = [0.2, -0.2] * 9
    assert marketreview.propose(flat)["action"] == "keep"


def test_scoring_scales_profit_by_the_factor():
    got = marketreview.score(0.5, [(1.0, -1.0), (1.0, 0.25)])
    assert got["actual"] == -0.75 and got["shadow"] == -0.375


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args=()):
        return SimpleNamespace(fetchall=lambda: self.rows)


def test_a_run_writes_proposals_then_scores_them_an_hour_later(tmp_path):
    now = 1_800_000_000.0
    losing = [row(ts=now - 3600 * 5 + i * 60, outcome="no") for i in range(20)]
    s, store = SimpleNamespace(data_dir=str(tmp_path)), SimpleNamespace(db=FakeDb(losing))
    marketreview._checked_at = 0.0
    first = marketreview.run(s, store, {"KXBTC15M": {"type": "choppy"}}, now=now)
    assert [p["action"] for p in first if p["kind"] == "proposal"] == ["stand_aside"] and first[0]["type"] == "choppy"
    marketreview._checked_at = 0.0
    later = marketreview.run(s, SimpleNamespace(db=FakeDb([*losing, row(ts=now + 600, outcome="no")])), None, now=now + 3700)
    scores = [p for p in later if p["kind"] == "score"]
    assert len(scores) == 1 and scores[0]["bets"] == 1.0 and scores[0]["actual"] == -1.0 and scores[0]["shadow"] == 0.0
    lines = [json.loads(x) for x in (tmp_path / "market_review.jsonl").read_text().splitlines()]
    assert [x["kind"] for x in lines].count("score") == 1
    marketreview._checked_at = 0.0
    again = marketreview.run(s, store, None, now=now + 7400)
    assert not [p for p in again if p["kind"] == "score" and p["ts"] == round(now)]  # a proposal is scored once
