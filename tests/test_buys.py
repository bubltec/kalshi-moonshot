from kalshi_moonshot.__main__ import format_open_buy
from kalshi_moonshot.store import Store


def _bet(store, ticker, ts, model_p, ask, context, title="Will Trump say it"):
    store.add_decision(
        ticker=ticker, title=title, ask=ask, bid=ask, model_p=model_p,
        edge=model_p - ask, action="bet", mode="trade", context=context, ts=ts,
    )


def test_open_buy_uses_the_decision_from_the_fill_not_a_later_rescore(tmp_path):
    store = Store(tmp_path / "m.db")
    _bet(store, "PEAC", 100, 0.12, 0.02, "- peace talks (1h ago)")
    store.add_order(
        ts=101, ticker="PEAC", env="prod", count=467, price=0.02,
        fill_count=467, cost_usd=9.95, fee_usd=0.61, side="buy",
    )
    _bet(store, "PEAC", 500, 0.25, 0.06, "- later headline")
    rows = store.open_positions("prod")
    assert len(rows) == 1
    assert rows[0]["model_p"] == 0.12
    assert rows[0]["ask"] == 0.02
    text = format_open_buy(rows[0])
    assert "model 0.12   ask 0.02   edge 0.10" in text
    assert "peace talks" in text
    assert "later headline" not in text


def test_a_settled_or_paper_bet_is_not_an_open_buy(tmp_path):
    store = Store(tmp_path / "m.db")
    _bet(store, "DONE", 100, 0.12, 0.02, "- old")
    store.add_order(
        ts=101, ticker="DONE", env="prod", count=10, price=0.02,
        fill_count=10, cost_usd=0.2, fee_usd=0.01, side="buy",
    )
    store.set_outcome("DONE", "no")
    _bet(store, "PAPER", 100, 0.12, 0.02, "- paper only")
    assert store.open_positions("prod") == []
