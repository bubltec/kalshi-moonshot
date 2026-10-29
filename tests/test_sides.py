from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import config, report, runner
from kalshi_moonshot.kalshi import _as_no
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import flip, side_ask_notional, side_prob


def test_flip_gives_the_no_side_view_of_a_market():
    m = {"yes_bid_dollars": "0.0500", "yes_ask_dollars": "0.0800", "ticker": "T"}
    f = flip(m)
    assert float(f["yes_ask_dollars"]) == pytest.approx(0.95)  # NO ask = 1 - YES bid
    assert float(f["yes_bid_dollars"]) == pytest.approx(0.92)  # NO bid = 1 - YES ask
    assert f["ticker"] == "T" and m["yes_bid_dollars"] == "0.0500"  # original untouched


def test_flip_keeps_missing_quotes_missing():
    no_bid = flip({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0800"})
    assert float(no_bid["yes_ask_dollars"]) == 0.0  # no YES bid: nothing to buy NO from
    sold_out = flip({"yes_bid_dollars": "0.9000", "yes_ask_dollars": "1.0000"})
    assert float(sold_out["yes_bid_dollars"]) == 0.0  # YES at $1: no NO bid to exit into


def test_side_prob():
    assert side_prob(0.8, "yes") == 0.8
    assert side_prob(0.8, "no") == pytest.approx(0.2)


def test_no_depth_comes_from_yes_bids():
    book = {"orderbook_fp": {"yes_dollars": [["0.9500", "100.00"], ["0.5000", "50.00"]],
                             "no_dollars": [["0.0500", "10.00"]]}}
    # a YES bid at 0.95 is NO for sale at 0.05: 100 contracts * $0.05
    assert side_ask_notional(book, 0.06, "no") == pytest.approx(5.0)
    # a NO bid at 0.05 is YES for sale at 0.95: 10 contracts * $0.95
    assert side_ask_notional(book, 0.96, "yes") == pytest.approx(9.5)
    assert side_ask_notional(book, 0.03, "no") == 0.0


def test_no_fill_prices_are_flipped_back_to_the_no_price():
    resp = _as_no({"fill_count": 3, "average_fill_price": "0.0500", "order_id": "x"})
    assert resp["average_fill_price"] == pytest.approx(0.95)
    assert _as_no({"fill_count": 0})["fill_count"] == 0  # nothing to flip


def row(side, outcome, ask=0.9, exit_price=None, model_p=0.99):
    return {"ticker": f"T-{side}-{outcome}", "model_p": model_p, "ask": ask, "outcome": outcome,
            "action": "bet", "side": side, "exit_price": exit_price, "lane": "commodity",
            "category": "Commodities"}


def test_a_no_bet_wins_when_the_market_resolves_no():
    rows = [row("no", "no"), row("yes", "yes"), row("no", "yes"), row("yes", "no")]
    s = report.summarize(rows)
    assert s["bets"] == 4 and s["bet_wins"] == 2
    won = report.summarize([row("no", "no")])
    lost = report.summarize([row("no", "yes")])
    assert won["bet_pnl_per_contract_set"] > 0 > lost["bet_pnl_per_contract_set"]


def test_old_rows_without_a_side_still_score_as_yes():
    old = {"ticker": "T", "model_p": 0.2, "ask": 0.1, "outcome": "yes", "action": "bet",
           "lane": "moonshot", "category": "", "exit_price": None}
    assert report.summarize([old])["bet_wins"] == 1


def test_calibration_scores_a_no_book_against_no_outcomes(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(30):  # NO side at 0.90, model says 0.99, and NO really wins 60% of the time
        store.add_decision(
            ticker=f"KXC-{i}", title="t", bid=0.9, ask=0.9, model_p=0.99, edge=0.0, action="bet",
            mode="observe", lane="commodity", category="Commodities", side="no",
        )
        store.set_outcome(f"KXC-{i}", "no" if i % 5 < 3 else "yes")
    cal = store.calibrator.adjust("commodity", "Commodities", 0.9, 0.99)
    assert cal.skill is not None and cal.skill < 0  # the record punishes an overconfident model
    assert cal.p < 0.9


def settings(tmp_path, **over):
    s = config.load()
    return type(s)(**{**s.__dict__, "data_dir": tmp_path, "mode": "observe", "env": "demo",
                      "stop_loss_enabled": True, "stop_loss_hours": 1.0, "stop_loss_drop": 0.3, **over})


class StopClient:
    def __init__(self, yes_bid, yes_ask):
        self.m = {"yes_bid_dollars": yes_bid, "yes_ask_dollars": yes_ask}
        self.sold_no = []
        self.sold_yes = []

    def market(self, ticker):
        return dict(self.m)

    def sell_no_ioc(self, ticker, count, price, client_order_id=None):
        self.sold_no.append((ticker, count, price))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sold_yes.append((ticker, count, price))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}


def open_bet(store, side, ask):
    close = (datetime.now(UTC) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.add_decision(
        ticker="KXC-1", title="t", bid=ask - 0.02, ask=ask, model_p=0.99, edge=0.05, action="bet",
        mode="trade", close_time=close, lane="commodity", side=side,
    )


def test_stop_loss_exits_a_no_position_by_its_own_price(tmp_path):
    s = settings(tmp_path, mode="trade", env="demo")
    store = Store(s.db_path)
    open_bet(store, "no", 0.90)
    store.add_order(ticker="KXC-1", env="demo", count=2, price=0.90, fill_count=2,
                    cost_usd=1.8, fee_usd=0.0, contract="no")
    # YES jumped to 0.80/0.85, so NO is now worth about 0.15-0.20: down >30% from 0.90
    client = StopClient(yes_bid="0.8000", yes_ask="0.8500")
    notes = runner.stop_loss_pass(s, client, store)
    assert client.sold_yes == [] and len(client.sold_no) == 1
    assert client.sold_no[0][2] == pytest.approx(0.10)  # NO bid = 1 - YES ask = 0.15, a stop-loss order is limited 5c under it
    assert any("sold" in n for n in notes)


def test_the_sweep_leaves_a_loss_to_the_socket_while_the_socket_covers_it(tmp_path):
    s = settings(tmp_path, mode="trade", env="demo")
    store = Store(s.db_path)
    open_bet(store, "no", 0.90)
    store.add_order(ticker="KXC-1", env="demo", count=2, price=0.90, fill_count=2,
                    cost_usd=1.8, fee_usd=0.0, contract="no")
    covered = StopClient(yes_bid="0.8000", yes_ask="0.8500")
    assert runner.stop_loss_pass(s, covered, store, socket_covers=lambda ticker, lane: True) == []
    assert covered.sold_no == []  # the socket decides this one
    uncovered = StopClient(yes_bid="0.8000", yes_ask="0.8500")
    runner.stop_loss_pass(s, uncovered, store, socket_covers=lambda ticker, lane: False)
    assert len(uncovered.sold_no) == 1  # the socket is not covering it: the backstop sells


def test_stop_loss_leaves_a_no_position_alone_when_it_is_winning(tmp_path):
    s = settings(tmp_path, mode="trade", env="demo")
    store = Store(s.db_path)
    open_bet(store, "no", 0.90)
    store.add_order(ticker="KXC-1", env="demo", count=2, price=0.90, fill_count=2,
                    cost_usd=1.8, fee_usd=0.0, contract="no")
    client = StopClient(yes_bid="0.0200", yes_ask="0.0400")  # NO worth ~0.96-0.98
    assert runner.stop_loss_pass(s, client, store) == []
    assert client.sold_no == []


# ---- moonshots are about price and payout, not which contract carries them ----
def test_a_cheap_no_is_a_moonshot_too():
    from kalshi_moonshot import lanes, strategy

    s = config.load()
    assert lanes.spec_for(s, "moonshot").sides == ("yes", "no")
    now = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    close = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    heavy_favorite = {
        "ticker": "KXFAV-1", "title": "Will the favorite win?", "market_type": "binary",
        "status": "active", "yes_bid_dollars": "0.9400", "yes_ask_dollars": "0.9600",
        "volume_fp": "900", "close_time": close, "event_ticker": "KXFAV",
    }
    s2 = type(s)(**{**s.__dict__, "skip_categories": frozenset(), "min_price": 0.02, "max_price": 0.10,
                    "min_volume": 100.0, "max_spread": 0.03, "min_hours_to_close": 2.0,
                    "max_hours_to_close": 48.0})
    shots = strategy.long_shots([heavy_favorite], s2, now)
    assert [(x.side, round(x.ask, 2)) for x in shots] == [("no", 0.06)]  # 6c to win 94c if it upsets


def test_trend_of_a_no_contract_is_read_from_the_yes_bid():
    from kalshi_moonshot import strategy

    candles = [{"yes_bid": {"close_dollars": str(b)}, "yes_ask": {"close_dollars": "0.9"}}
               for b in (0.90, 0.88, 0.86, 0.84)]
    yes = strategy.ask_trend(candles, "yes")
    no = strategy.ask_trend(candles, "no")
    assert yes.change == 0.0  # the YES ask did not move
    assert no.change == pytest.approx(0.06)  # the NO ask rose as the YES bid fell


def test_sides_setting_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("MOONSHOT_SIDES", "maybe")
    with pytest.raises(ValueError, match="MOONSHOT_SIDES"):
        config.load()
    monkeypatch.setenv("MOONSHOT_SIDES", "yes")
    assert config.load().moonshot_sides == ("yes",)


# ---- guaranteed-profit pairs ----
def ladder_market(strike, bid, ask, event="KXA-1", series_t=True):
    return {"ticker": f"{event}-T{strike}" if series_t else f"{event}-X{strike}", "event_ticker": event,
            "strike_type": "greater", "floor_strike": float(strike),
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "volume_fp": "100"}


def test_a_normal_ladder_has_no_free_lunch():
    from kalshi_moonshot.strategy import ladder_arbs

    assert ladder_arbs([ladder_market(100, 0.70, 0.72), ladder_market(110, 0.40, 0.42),
                        ladder_market(120, 0.10, 0.12)]) == []


def test_a_crossed_ladder_is_found_and_priced_after_fees():
    from kalshi_moonshot.strategy import ladder_arbs

    # YES above 100 offered at 0.40 while YES above 110 is bid at 0.55: higher strike valued more
    arbs = ladder_arbs([ladder_market(100, 0.38, 0.40), ladder_market(110, 0.55, 0.57)])
    assert len(arbs) == 1
    a = arbs[0]
    assert (a.low, a.high) == ("KXA-1-T100", "KXA-1-T110")
    assert a.net == pytest.approx(0.55 - 0.40 - 0.07 * 0.4 * 0.6 - 0.07 * 0.45 * 0.55, abs=1e-6)
    assert a.net > 0


def test_a_thin_crossing_is_eaten_by_fees():
    from kalshi_moonshot.strategy import ladder_arbs

    assert ladder_arbs([ladder_market(100, 0.38, 0.50), ladder_market(110, 0.51, 0.53)]) == []


def test_only_real_ladders_count_not_spreads_or_brackets():
    from kalshi_moonshot.strategy import ladder_arbs

    spread = [ladder_market(2, 0.02, 0.04, event="KXTEAM", series_t=False),
              ladder_market(3, 0.60, 0.62, event="KXTEAM", series_t=False)]
    assert ladder_arbs(spread) == []  # opposite-team spreads do not nest


# ---- the stop-loss and 15-minute windows ----
def fifteen_bet(store, ticker, side="yes", ask=0.80):
    close = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.add_decision(ticker=ticker, title="t", bid=ask - 0.02, ask=ask, model_p=0.95, edge=0.05, action="bet",
                       mode="observe", close_time=close, lane="commodity", side=side)


def test_the_stop_loss_can_leave_15_minute_windows_to_settle(tmp_path):
    s_on = settings(tmp_path, stop_loss_fast=True)
    s_off = settings(tmp_path, stop_loss_fast=False)
    store = Store(s_on.db_path)
    fifteen_bet(store, "KXGOLD15M-X-15")
    fifteen_bet(store, "KXGOLDH-Y-T4000")

    class C:
        def market(self, ticker):
            return {"yes_bid_dollars": "0.3000", "yes_ask_dollars": "0.3200"}  # down 62% from 0.80

    on = runner.stop_loss_pass(s_on, C(), store)
    assert sum("paper-sold" in n for n in on) == 2  # both are stopped out today
    store = Store(tmp_path / "off.db")
    fifteen_bet(store, "KXGOLD15M-X-15")
    fifteen_bet(store, "KXGOLDH-Y-T4000")
    off = runner.stop_loss_pass(s_off, C(), store)
    assert [n for n in off if "KXGOLD15M" in n] == []  # the 15-minute window rides to settlement
    assert any("KXGOLDH" in n and "paper-sold" in n for n in off)  # an hourly market still gets its stop


def test_the_default_keeps_todays_stop_loss_behavior(monkeypatch):
    monkeypatch.delenv("STOP_LOSS_15M", raising=False)
    assert config.load().stop_loss_fast is True
    monkeypatch.setenv("STOP_LOSS_15M", "false")
    assert config.load().stop_loss_fast is False
