import time

import pytest

from kalshi_moonshot import config, manual, watcher
from kalshi_moonshot.store import Store


class Client:
    authed = True

    def __init__(self, bid=0.60, positions=None):
        self.bid, self.sold = bid, []
        self.rows = positions if positions is not None else [
            {"ticker": "KXWTI-26OCT2214-T98.99", "position_fp": "4.00", "market_exposure_dollars": "1.60"},  # YES paid 0.40
            {"ticker": "KXNASDAQ100U-X", "position_fp": "-10.00", "market_exposure_dollars": "4.60"},  # NO paid 0.46
            {"ticker": "KXOLD", "position_fp": "0.00", "market_exposure_dollars": "0.00"},
        ]

    def positions(self):
        return {"market_positions": self.rows}

    def market(self, ticker):
        return {"ticker": ticker, "yes_bid_dollars": str(self.bid), "yes_ask_dollars": str(self.bid + 0.01),
                "close_time": "2026-10-22T20:00:00Z", "event_ticker": "KXWTI-26OCT2214"}

    def _sell(self, side, ticker, count, price):
        self.sold.append((side, ticker, count, price))
        return {"fill_count": count, "average_fill_price": price}

    def sell_yes_ioc(self, ticker, count, price):
        return self._sell("yes", ticker, count, price)

    def sell_no_ioc(self, ticker, count, no_price):
        return self._sell("no", ticker, count, no_price)


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MANUAL_EXIT", "true")
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    return config.load()


def test_positions_parse_side_count_and_price_paid():
    got = manual.parse_positions(Client().positions())
    assert [(p.ticker, p.side, p.count) for p in got] == [("KXWTI-26OCT2214-T98.99", "yes", 4.0), ("KXNASDAQ100U-X", "no", 10.0)]
    assert got[0].entry == pytest.approx(0.40) and got[1].entry == pytest.approx(0.46)


def test_the_watcher_follows_hand_placed_positions_only_when_asked(s, monkeypatch):
    c = Client()
    w = watcher.ProfitWatcher(lambda: s, c, lambda: Store(s.db_path))
    w.refresh()
    assert {t for (t, lane) in w.targets if lane == "manual"} == {"KXWTI-26OCT2214-T98.99", "KXNASDAQ100U-X"}
    assert w.manual_tickers() == {"KXWTI-26OCT2214-T98.99", "KXNASDAQ100U-X"}
    monkeypatch.setenv("MANUAL_EXIT", "false")
    off = config.load()
    w2 = watcher.ProfitWatcher(lambda: off, c, lambda: Store(off.db_path))
    w2.refresh()
    assert w2.manual_tickers() == set()


def test_skipped_prefixes_and_bot_bets_are_left_alone(s, monkeypatch):
    monkeypatch.setenv("MANUAL_EXIT_SKIP", "KXNASDAQ")
    s2 = config.load()
    store = Store(s2.db_path)
    store.add_order(ticker="KXWTI-26OCT2214-T98.99", env=s2.env, count=4, price=0.40, fill_count=4, cost_usd=1.60, fee_usd=0.0, side="buy")
    assert manual.ManualPositions(Client()).positions(s2, store, fresh=True) == []


def test_a_peak_crash_sells_a_hand_placed_position_for_a_profit(s):
    c = Client(bid=0.55)
    w = watcher.ProfitWatcher(lambda: s, c, lambda: Store(s.db_path))
    w.refresh()
    t = w.targets[("KXWTI-26OCT2214-T98.99", "manual")]
    t.peak, t.last_bid, t.last_ts = 0.62, 0.55, time.time()
    w._sell("KXWTI-26OCT2214-T98.99", "manual", "peak-crash: bid 0.55 is 0.07 off its 0.62 peak (paid 0.40) for 11s")
    assert [(x[0], x[1], x[2]) for x in c.sold] == [("yes", "KXWTI-26OCT2214-T98.99", 4.0)]
    assert c.sold[0][3] == pytest.approx(0.53)  # the 2c cushion under the bid


def test_a_hand_placed_position_is_never_sold_at_or_under_cost(s):
    c = Client(bid=0.41)  # barely over the 0.40 paid: the fee and margin eat it
    w = watcher.ProfitWatcher(lambda: s, c, lambda: Store(s.db_path))
    w.refresh()
    w._sell("KXWTI-26OCT2214-T98.99", "manual", "peak-crash: bid 0.41 is 0.12 off its 0.53 peak (paid 0.40), fast exit")
    assert c.sold == []


def test_nothing_is_sold_when_trading_is_off(s, monkeypatch):
    monkeypatch.setenv("MODE", "observe")
    off = config.load()
    c = Client(bid=0.60)
    w = watcher.ProfitWatcher(lambda: off, c, lambda: Store(off.db_path))
    w.refresh()
    w._sell("KXWTI-26OCT2214-T98.99", "manual", "peak-crash: bid 0.60 is 0.05 off its 0.65 peak (paid 0.40) for 11s")
    assert c.sold == []


def test_the_minimum_profit_is_respected(s, monkeypatch):
    monkeypatch.setenv("MANUAL_EXIT_MIN_PROFIT", "0.30")
    strict = config.load()
    for bid, sells in ((0.50, False), (0.60, True)):  # paid 0.40: a 30% minimum needs about 0.52 plus the fee
        c = Client(bid=bid)
        w = watcher.ProfitWatcher(lambda: strict, c, lambda: Store(strict.db_path))
        w.refresh()
        w._sell("KXWTI-26OCT2214-T98.99", "manual", "peak-crash: bid is 0.05 off its peak (paid 0.40) for 11s")
        assert bool(c.sold) is sells, bid


def stop_watcher(s, bid, held=None):
    c = Client(bid=bid)
    w = watcher.ProfitWatcher(lambda: s, c, lambda: Store(s.db_path), hold_value=(lambda row, st: held))
    w.refresh()
    return c, w


def test_a_hand_placed_position_gets_a_stop_that_runs_only_near_the_close(s):
    _c, w = stop_watcher(s, 0.40)
    t = w.targets[("KXWTI-26OCT2214-T98.99", "manual")]
    assert t.stop_drop == pytest.approx(0.30)
    assert t.stop_from == pytest.approx(__import__("datetime").datetime(2026, 10, 22, 20, 0, tzinfo=__import__("datetime").UTC).timestamp() - 3600)


def test_a_stop_loss_sells_a_hand_placed_position_below_cost_when_the_model_agrees(s):
    why = "stop-loss: bid 0.25 is 38% under the 0.40 paid (socket)"
    c, w = stop_watcher(s, 0.25, held=0.20)  # worth less than the 0.25 bid: sell
    w._sell("KXWTI-26OCT2214-T98.99", "manual", why)
    assert len(c.sold) == 1 and c.sold[0][3] == pytest.approx(0.20)  # the bid less 2c and 3c of cushion
    c, w = stop_watcher(s, 0.25, held=None)  # no model: sell
    w._sell("KXWTI-26OCT2214-T98.99", "manual", why)
    assert len(c.sold) == 1


def test_a_stop_loss_holds_a_dip_the_model_still_likes(s):
    c, w = stop_watcher(s, 0.25, held=0.80)
    w._sell("KXWTI-26OCT2214-T98.99", "manual", "stop-loss: bid 0.25 is 38% under the 0.40 paid (socket)")
    assert c.sold == []


def test_an_impossible_minimum_profit_warns_and_never_sells(s, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("MANUAL_EXIT_MIN_PROFIT", "2.0")
    big = config.load()
    c, w = stop_watcher(big, 0.90)
    with caplog.at_level(logging.WARNING, logger="moonshot"):
        w._sell("KXWTI-26OCT2214-T98.99", "manual", "peak-crash: bid 0.90 is 0.05 off its 0.95 peak (paid 0.40) for 11s")
    assert c.sold == [] and "can never sell" in caplog.text


def test_adding_to_a_position_moves_the_price_paid_to_the_average(s):
    c = Client(bid=0.40)
    w = watcher.ProfitWatcher(lambda: s, c, lambda: Store(s.db_path))
    w.refresh()
    key = ("KXWTI-26OCT2214-T98.99", "manual")
    assert w.targets[key].entry == pytest.approx(0.40)
    c.rows[0] = {"ticker": "KXWTI-26OCT2214-T98.99", "position_fp": "8.00", "market_exposure_dollars": "2.80"}  # a second buy lowered the average to 0.35
    w.manual._at = 0.0  # force a re-read
    w.refresh()
    assert w.targets[key].entry == pytest.approx(0.35)


def quote(bid, ts):
    return {"kind": "quote", "ticker": "KXWTI-26OCT2214-T98.99", "bid": bid, "ask": bid + 0.01, "ts": ts}


def test_the_stop_waits_for_the_price_to_turn_and_ignores_the_spread(s, monkeypatch):
    monkeypatch.setenv("MANUAL_EXIT_STOP_CONFIRM", "20")
    cfg = config.load()
    _c, w = stop_watcher(cfg, 0.30)
    t = w.targets[("KXWTI-26OCT2214-T98.99", "manual")]
    t.stop_from = 0.0  # as if inside the last hour
    base = time.time()
    w.on_event(quote(0.30, base))  # bought at 0.40, but the bid is already 0.30: that spread is the mark, not a loss
    assert t.stop_ref == pytest.approx(0.30) and w._jobs.empty()
    w.on_event(quote(0.24, base + 1))  # 20% under the mark: no stop
    assert w._jobs.empty()
    w.on_event(quote(0.20, base + 2))  # 33% under the mark, but only just: wait
    assert w._jobs.empty()
    w.on_event(quote(0.21, base + 15))  # still under, not yet 20 s
    assert w._jobs.empty()
    w.on_event(quote(0.20, base + 23))  # 21 s under the line
    assert not w._jobs.empty() and "mark" in w._jobs.get_nowait()[2]


def test_a_recovery_resets_the_stop_clock(s, monkeypatch):
    monkeypatch.setenv("MANUAL_EXIT_STOP_CONFIRM", "20")
    cfg = config.load()
    _c, w = stop_watcher(cfg, 0.40)
    t = w.targets[("KXWTI-26OCT2214-T98.99", "manual")]
    t.stop_from = 0.0
    base = time.time()
    w.on_event(quote(0.40, base))
    w.on_event(quote(0.26, base + 1))  # under the line
    w.on_event(quote(0.35, base + 10))  # the price turned
    w.on_event(quote(0.26, base + 15))  # under again: the clock restarts
    w.on_event(quote(0.26, base + 30))
    assert w._jobs.empty()


def test_a_cheap_ticket_below_even_odds_is_held_when_the_model_values_it_above_the_bid(s):
    c, w = stop_watcher(s, 0.28, held=0.38)  # paid 0.40... a 0.38 hold value beats a 0.28 bid
    w._sell("KXWTI-26OCT2214-T98.99", "manual", "stop-loss: bid 0.28 is 30% under the 0.40 mark (socket)")
    assert c.sold == []
    c, w = stop_watcher(s, 0.28, held=0.20)  # worth less than the bid: sell
    w._sell("KXWTI-26OCT2214-T98.99", "manual", "stop-loss: bid 0.28 is 30% under the 0.40 mark (socket)")
    assert len(c.sold) == 1


def test_a_paper_bet_on_the_same_market_does_not_hide_a_hand_placed_position(s):
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXWTI-26OCT2214-T98.99", "bet", "observe", 0.3, "no", "steady_raw", 3, 1.0, "2099-01-01T00:00:00Z"))
    store.db.commit()
    assert "KXWTI-26OCT2214-T98.99" in {p.ticker for p in manual.ManualPositions(Client()).positions(s, store, fresh=True)}


def test_only_the_part_of_a_position_you_bought_is_yours(s):
    store = Store(s.db_path)
    # the bot bought 3 of the same NO contracts for $0.75; the account holds 10 in all ($4.60 in)
    store.add_order(ticker="KXNASDAQ100U-X", env=s.env, count=3, price=0.25, fill_count=3, cost_usd=0.75, fee_usd=0.0, side="buy")
    got = {p.ticker: p for p in manual.ManualPositions(Client()).positions(s, store, fresh=True)}
    p = got["KXNASDAQ100U-X"]
    assert p.count == pytest.approx(7.0) and p.entry == pytest.approx((4.60 - 0.75) / 7.0)
    store.add_order(ticker="KXWTI-26OCT2214-T98.99", env=s.env, count=4, price=0.40, fill_count=4, cost_usd=1.60, fee_usd=0.0, side="buy")
    assert "KXWTI-26OCT2214-T98.99" not in {q.ticker for q in manual.ManualPositions(Client()).positions(s, store, fresh=True)}  # all the bot's
