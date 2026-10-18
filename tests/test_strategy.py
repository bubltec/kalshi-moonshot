from datetime import UTC, datetime

from kalshi_moonshot import config, strategy

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def settings(**over):
    s = config.load()
    return type(s)(**{**s.__dict__, **over})


def market(**over):
    m = {
        "ticker": "T1", "title": "Will X?", "market_type": "binary", "status": "active",
        "yes_bid_dollars": "0.04", "yes_ask_dollars": "0.05", "volume_fp": "500.00",
        "close_time": "2026-10-25T12:00:00Z", "rules_primary": "rules",
    }
    m.update(over)
    return m


def test_good_longshot_qualifies():
    assert strategy.reject_reason(market(), settings(), NOW) is None


def test_sports_category_is_skipped():
    s = settings()
    assert strategy.reject_reason(market(category="Sports"), s, NOW) == "category skipped"
    assert strategy.reject_reason(market(category="sports"), s, NOW) == "category skipped"
    assert strategy.reject_reason(market(category="Politics"), s, NOW) is None


def test_no_bid_is_rejected_because_it_cannot_be_exited():
    m = market(yes_bid_dollars="0.0000")
    assert strategy.reject_reason(m, settings(), NOW) == "no bid (cannot exit)"


def test_rejects_expensive_thin_wide_and_soon():
    s = settings()
    assert strategy.reject_reason(market(yes_ask_dollars="0.40", yes_bid_dollars="0.39"), s, NOW)
    assert strategy.reject_reason(market(volume_fp="3.00"), s, NOW) == "volume too low"
    assert strategy.reject_reason(market(yes_bid_dollars="0.01"), s, NOW) == "spread too wide"
    assert strategy.reject_reason(market(close_time="2026-10-17T13:00:00Z"), s, NOW) == "closes too soon"


def test_long_shots_sorted_by_volume():
    ms = [market(ticker="A", volume_fp="200"), market(ticker="B", volume_fp="900")]
    assert [x.ticker for x in strategy.long_shots(ms, settings(), NOW)] == ["B", "A"]


def test_edge_subtracts_fee():
    # 5c ask: fee per contract = 0.07*0.05*0.95 = 0.003325
    assert abs(strategy.edge(0.10, 0.05) - (0.10 - 0.05 - 0.003325)) < 1e-9


def test_budget_sizing_never_exceeds_budget():
    for ask in (0.02, 0.05, 0.09):
        n = strategy.contracts_for_budget(5.0, ask)
        assert n > 0
        assert ask * n + strategy.order_fee(ask, n) <= 5.0 + 1e-9
