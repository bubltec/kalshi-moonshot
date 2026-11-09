"""Settings that compare against the size of our own bet are shares of it, not flat dollars: the burst hedge, the depth check, the per-trade ceiling and the daily loss limit."""
import time

from kalshi_moonshot import config, lanes, risk, runner
from kalshi_moonshot.sizing import size_bet
from kalshi_moonshot.store import Store
from tests.test_buy_retry import setup  # noqa: F401  (the buy fixture)
from tests.test_risk import lose


def _settings(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return config.load()








def test_the_per_trade_ceiling_is_a_share_of_equity_when_set(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, MAX_TRADE_PCT="0.04", MAX_USD_PER_TRADE="100")
    spec = lanes.spec_for(s, "moonshot")
    small = size_bet(spec, s, p=0.20, ask=0.05, available=1000, equity=100, lane_exposure=0)
    big = size_bet(spec, s, p=0.20, ask=0.05, available=1000, equity=1000, lane_exposure=0)
    assert small.cost_usd <= 4.0 + 1e-9 and big.cost_usd <= 40.0 + 1e-9  # 4% of each account, not a flat dollar number


def test_the_risk_gate_trusts_a_sized_order_under_the_share_cap_and_falls_back_without_equity(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, MAX_TRADE_PCT="0.10", MAX_USD_PER_TRADE="10")
    store = Store(s.db_path)
    assert risk.check(s, store, "A", 15.0, equity=500.0).ok  # 10% of $500 is $50, over the old flat $10
    assert not risk.check(s, store, "A", 15.0, equity=100.0).ok  # 10% of $100 is $10
    assert not risk.check(s, store, "A", 15.0).ok  # no equity read: the flat dollar cap


def test_the_daily_loss_limit_is_a_share_of_equity_when_set(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, MAX_LOSS_PCT_PER_DAY="0.05", MAX_LOSS_PER_DAY="500")
    store = Store(s.db_path)
    now = time.time()
    lose(store, "L1", 4.0, now - 60)
    assert risk.check(s, store, "NEW", 1.0, now=now, equity=100.0).ok  # down $4 of a $5 limit
    lose(store, "L2", 1.5, now - 30)
    v = risk.check(s, store, "NEW", 1.0, now=now, equity=100.0)
    assert not v.ok and "limit $5.00" in v.reason
    assert risk.check(s, store, "NEW", 1.0, now=now, equity=1000.0).ok  # 5% of a bigger account is $50


def test_the_depth_check_is_a_share_of_the_order(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch, MIN_OFFER_RATIO="0.5")
    assert s.min_offer_ratio == 0.5 and s.min_order_usd == 0.25
    assert runner._cap_equity(s, None, None) == 0.0  # no share cap is set, so no balance read


class _ThinBook:
    """A buy client whose book shows only 1.0 contract at 0.30, about $0.30 offered, however big the order is."""

    def __init__(self, inner):
        self.inner = inner

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.7000", "1.00"]]}}

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_thin_book_passes_the_flat_minimum_but_not_a_share_of_a_big_order(setup):  # noqa: F811
    import dataclasses

    from tests.test_buy_retry import Client

    s, store, shot, spec = setup
    spec()
    flat = runner.place(s, _ThinBook(Client(fills=[3.0], asks=[0.30])), store, shot, "why", 3.0)
    assert flat.filled == 3.0  # $0.30 offered clears the flat $0.25, whatever the order
    ratio = dataclasses.replace(s, min_offer_ratio=0.5)
    got = runner.place(ratio, _ThinBook(Client(fills=[3.0], asks=[0.30])), store, shot, "why", 3.0)
    assert got.filled == 0 and "needed" in got.note and "50%" in got.note  # a $0.93 order needs $0.47 offered


class _RanAwayBook:
    """A book whose cheapest YES offer (1 - the best NO bid of 0.56) is 0.44, over the 0.30 ask the lane decided on."""

    def __init__(self, inner):
        self.inner = inner

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.5600", "50.00"]]}}

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_skip_for_nothing_offered_says_when_the_ask_ran_away(setup):  # noqa: F811
    import dataclasses

    from tests.test_buy_retry import Client

    s, store, shot, spec = setup
    spec()
    ratio = dataclasses.replace(s, min_offer_ratio=0.5)
    got = runner.place(ratio, _RanAwayBook(Client(fills=[3.0], asks=[0.30])), store, shot, "why", 3.0)
    assert got.filled == 0 and "$0.00 offered" in got.note and "the yes ask is now 0.44" in got.note and "ran away" in got.note
