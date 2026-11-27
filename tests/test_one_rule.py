"""The one exit rule (fallrule.py; user, 2026-11-26: "we need to consolidate one rule that handles them all"): early profit that fades with the time elapsed, no loss while the spot has not crossed, salvage on a failed bounce, the time backstop and the top. Unit tests on the pure function, then the watcher's step through a held bet."""
from kalshi_moonshot import fallrule, memguard
from kalshi_moonshot.strategy import fee_per_contract as fee


def asker(entry, wobble=0.043, spot_unit=10.0, bought=1e-9):
    """`bought` is the bet's buy time on a clock where the market opens at 0 and closes at 900 (the default: bought at the open, so the bet's age is the market's clock)."""
    s = fallrule.State()

    def ask(bid, left, cushion=None, cross=None):
        return fallrule.decide(s, bid, entry, fee(bid), fee(entry), fee, wobble, 900.0 - left, left, cushion, spot_unit if cushion is not None else None, cross, None, bought)

    ask.state = s
    return ask


def test_early_profit_sells_btc_1700_at_a_gain_on_the_tapes_own_bids():
    """BTC 17:00 YES, 2026-11-26: bought 0.64, the bid at 0.74 to 0.77 with 590 s left, then 0.72 -> 0.65 in a second; sold at 0.07 by no_recovery. The fall allowed is never more than the profit."""
    ask = asker(0.64)
    assert not ask(0.74, 590, 20.0).sell  # +7c after both fees: a profit run starts
    assert not ask(0.77, 588, 20.0).sell
    assert not ask(0.74, 576, 20.0).sell  # under a wobble and a half: the climb goes on
    assert not ask(0.72, 565, 20.0).sell
    r = ask(0.71, 563, 20.0)
    assert r.sell and r.kind == "fall" and r.net > 0


def test_a_profit_under_a_wobble_is_not_a_profit_and_a_strong_cushion_holds():
    ask = asker(0.64)
    assert ask(0.68, 600, 5.0).kind == "nothing"
    ask = asker(0.64)
    assert not ask(0.80, 600, 5.0).sell
    held = ask(0.70, 590, 40.0)  # 40 against 10 x sqrt(590/60) = 31: over a sigma of the time left
    assert not held.sell and held.kind == "cushion"
    assert ask(0.70, 589, 5.0).sell  # the same fall without the cushion


def test_early_profit_fades_with_the_time_elapsed_and_has_no_line():
    """User: "the early profit concept needs to expire but not with a hard stop it should just approach no action after about 10 mins elapsed"."""
    fades = [fallrule.fade(60.0 * m) for m in range(0, 15)]  # by the bet's age in minutes
    assert all(a >= b for a, b in zip(fades, fades[1:], strict=False))  # it only ever weakens
    assert fades[5] > 0.95 and 0.05 < fades[10] < 0.2 and fades[12] < 0.01
    ask = asker(0.64)
    assert not ask(0.80, 240, 5.0).sell  # minute 11: a profit run
    late = ask(0.72, 235, 5.0)  # the same 8c fall that sells at minute 5 is left alone
    assert not late.sell and late.kind == "under bar"
    assert not ask(0.72, 150, 5.0).sell  # and a stall is not sold either


def test_late_profit_is_not_sold_unless_the_spot_is_under_the_target():
    """User, 2026-11-26: "make sure late profit doesn't sell if it's not under the target. We keep losing a ton late market from sales for no reason"."""
    ask = asker(0.64)
    assert not ask(0.90, 120, 3.0).sell
    assert not ask(0.75, 110, 3.0).sell  # fell 15c late with the spot still over the target: held
    assert not ask(0.75, 100, -1.0).sell  # a hair under the target is not a cross (a wobble of the spot is 4.1)
    r = ask(0.75, 95, -6.0)  # the spot is a wobble under the target: the profit is taken
    assert r.sell and r.kind == "fall" and r.net > 0


def test_no_loss_while_the_spot_has_not_crossed_near_2100():
    """NEAR 21:00 YES, 2026-11-26: bought 0.82, sold at 0.46 by no_recovery with the spot's average 0.01 sigma under the target; it settled YES."""
    ask = asker(0.82, wobble=0.05)
    assert not ask(0.82, 595, 2.0).sell
    assert not ask(0.53, 500, -0.01).sell
    assert not ask(0.67, 440, 1.0).sell  # a bounce
    assert not ask(0.54, 420, -0.01).sell  # the bounce failed, but the spot is a hair under: no sale
    r = ask(0.46, 258, -0.01)  # 341 s under cost: the time backstop does not sell either
    assert not r.sell and r.kind == "no cross"


def test_salvage_sells_the_failed_bounce_not_the_low_and_the_backstop_sells_no_bounce():
    ask = asker(0.78, wobble=0.05)
    assert not ask(0.80, 345, 2.0).sell
    assert not ask(0.57, 285, -8.0).sell  # the collapse, spot crossed: not sold on the way down
    assert not ask(0.15, 273, -8.0).sell  # the low
    assert not ask(0.30, 250, -8.0).sell  # bounced a bar off the low
    r = ask(0.20, 235, -8.0)  # the bounce failed
    assert r.sell and r.kind == "salvage" and r.net < 0
    ask = asker(0.80, wobble=0.05)
    assert not ask(0.78, 760, -8.0).sell
    assert not ask(0.60, 600, -8.0).sell  # sliding with no bounce
    r = ask(0.40, 460, -8.0)  # 300 s under cost since first seen, spot crossed
    assert r.sell and r.kind == "no recovery"


def test_the_cross_reads_the_better_of_two_cushions_and_the_top_sells():
    ask = asker(0.78, wobble=0.05)
    ask(0.15, 273, -8.0)
    ask(0.30, 250, -8.0)
    assert not ask(0.20, 235, -8.0, cross=1.0).sell  # the 5 s average is back over the target: not crossed
    assert ask(0.20, 234, -8.0, cross=-8.0).sell
    ask = asker(0.80)
    assert not ask(0.98, 60, 5.0).sell
    r = ask(0.99, 45, 5.0)
    assert r.sell and r.kind == "top"


def test_integration_the_watchers_step_sells_through_the_rule_and_names_what_it_read(tmp_path, monkeypatch):
    from kalshi_moonshot import curverisk, sellchain, watcher
    from tests.test_wave_top import take_target

    assert "one_rule" in sellchain.SELL_STEPS
    w, t, close = take_target(tmp_path, monkeypatch, 0.64, unit=0.1044)
    t.risk_unit, t.risk_unit_at = 0.1044, 1e12  # the cached unit: the bid's wobble is read from it
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: (20.0, 10.0))
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.66, 0.64, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_one_rule(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.74, 590) is None and ask(0.77, 588) is None and ask(0.72, 565) is None
    why = ask(0.71, 563)
    assert why and why.startswith("one rule, early profit:") and "sold at the bid 0.71" in why
    t.one_rule = None
    assert ask(0.60, 400) is None  # under cost with the spot on the bet's side: no loss
    assert ask(0.99, 300).startswith("one rule, the top:")  # outside the settlement window the top sells
    assert ask(0.99, 30) is None  # inside it, with the spot 20 over the target all window, the settlement is locked in: held


def test_the_top_holds_when_the_settlement_average_is_locked_in_and_sells_when_nothing_is_banked():
    """NEAR 21:45 YES, 2026-11-26: sold at 0.991 with 14 s left and settled YES; user: "still selling when it's like a 99% chance". ZEC 15:45 reached 0.99 with 60 s left and lost: nothing was banked, and that still sells."""
    z = fallrule.settle_z(5.0, 5.0, 10.0, 14.0)  # 46 s banked 5 over the target, spot 5 over now: it must fall 21.4, against a sigma of 4.8
    assert z is not None and z > 4
    ask = asker(0.80)
    held = fallrule.decide(ask.state, 0.991, 0.80, fee(0.991), fee(0.80), fee, 0.043, 886.0, 14.0, 5.0, 10.0, None, z)
    assert not held.sell and held.kind == "locked"
    assert fallrule.settle_z(5.0, 5.0, 10.0, 60.0) is None and fallrule.settle_z(None, 5.0, 10.0, 14.0) is None  # nothing banked, or unread
    thin = fallrule.settle_z(0.5, 0.2, 10.0, 40.0)  # barely over the target with 40 s left: far from locked
    sold = fallrule.decide(ask.state, 0.991, 0.80, fee(0.991), fee(0.80), fee, 0.043, 860.0, 40.0, 5.0, 10.0, None, thin)
    assert sold.sell and sold.kind == "top"
    assert fallrule.decide(ask.state, 0.991, 0.80, fee(0.991), fee(0.80), fee, 0.043, 840.0, 60.0, 5.0, 10.0).sell  # 60 s left: the top sells as before


def test_integration_a_rebuy_of_the_same_market_is_watched_as_a_new_bet(tmp_path, monkeypatch):
    """HYPE 21:45 YES, 2026-11-26: bought 0.61, sold 29 s later, bought again at 0.67 five seconds after that, before a refresh had dropped the first bet's target; the second bet was watched with the first one's price paid ("paid 0.61" in its sale's note), buy time and exit memory."""
    from kalshi_moonshot import config
    from kalshi_moonshot.store import Store
    from tests.test_risk_exit_flow import CLOSE, TICKER, held

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['one_rule']")
    first = w.targets[(TICKER, "risk_only")]
    first.one_rule = fallrule.State(run_high=0.9, first_ts=CLOSE - 450)
    assert abs(first.entry - 0.789) < 0.01 and first.side == "yes"
    s = config.load()
    st = Store(s.db_path)
    st.db.execute("UPDATE decisions SET exit_ts=?, exit_price=0.85 WHERE ticker=? AND action='bet'", (CLOSE - 300, TICKER))
    st.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CLOSE - 295, TICKER, "bet", "trade", 0.30, "no", "risk_only", 10.0, 3.0, "2030-01-01T00:15:00Z", None))
    st.db.execute(
        "INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd, order_id, side, contract) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CLOSE - 294, TICKER, s.env, 10.0, 0.30, 10.0, 3.0, 0.0, "o2", "buy", "no"))
    st.db.commit()
    w.refresh()  # no refresh ran between the sale and the new buy
    second = w.targets[(TICKER, "risk_only")]
    assert second is not first
    assert second.side == "no" and abs(second.entry - 0.30) < 1e-6 and second.bought_ts == CLOSE - 295 and second.one_rule is None
    w.refresh()
    assert w.targets[(TICKER, "risk_only")] is second  # the same bet keeps its target and its memory


def test_integration_a_loss_sale_by_the_one_rule_goes_through_the_selling_thread_to_an_order(tmp_path, monkeypatch):
    """BTC, ZEC and DOGE 22:30 NO, 2026-11-26: the one rule decided its salvage sales on the tick and the selling thread backed every one of them off, because a sale under cost is placed only when its reason starts with a phrase on the thread's list and "one rule" was not there; all three went to zero. Quotes in, an order out."""
    from kalshi_moonshot import watcher
    from tests.test_risk_exit_flow import TICKER, Client, held, live, signals, yes_bid

    live(monkeypatch)
    client = Client(bid=0.30, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['one_rule']")
    t = w.targets[(TICKER, "risk_only")]
    assert t.sell_chain == ("one_rule",)
    t.shock_index, t.shock_ref = "X", 100.0
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: (-8.0, 10.0))  # the spot is two wobbles of its own on the wrong side
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.12)  # the bid's wobble is 0.049
    got = []
    for k, bid in enumerate([0.60, 0.40, 0.20, 0.25, 0.33, 0.40, 0.45, 0.42, 0.36, 0.30, 0.30, 0.30]):  # the collapse, a bounce off 0.20 to 0.45, and the bounce failing
        yes_bid(w, bid, 400 - 3 * k)
        got += signals(w)
        if got:
            break
    assert got and got[0].why.startswith("one rule, salvage:"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert [n for n, _ in client.sold] == [4.65], "the selling thread did not place the one rule's loss sale"
    from kalshi_moonshot import config
    from kalshi_moonshot.store import Store

    assert len(Store(config.load().db_path).open_bets()) == 0  # and the bet is closed


def test_every_sell_step_declares_whether_it_may_sell_under_cost_and_the_selling_thread_reads_only_that():
    """User, 2026-11-26: "Make a note when adding rules that it must be in this list or set up a coding mechanism to make this automatic". A step with no entry stops the import; the selling thread's list is the table."""
    import inspect

    import pytest
    from kalshi_moonshot import sellchain, watcher

    assert set(sellchain.LOSS_REASONS) == set(sellchain.SELL_STEPS)
    assert sellchain.LOSS_PREFIXES == ("peak-crash", "stop-loss", "curve risk exit", "wave give-up", "value sale", "late dip", "no recovery", "one rule")  # the list as it stood, in the same order, now built from the table
    assert sellchain.may_sell_under_cost("one rule, salvage: under the price paid") and not sellchain.may_sell_under_cost("wave take: the bid fell")
    body = inspect.getsource(watcher.ProfitWatcher._sell)
    assert "sellchain.may_sell_under_cost(why)" in body and 'why.startswith(("peak-crash", "stop-loss", "curve risk exit"' not in body
    src = inspect.getsource(sellchain).replace("SELL_STEPS = (", "SELL_STEPS = ('a_new_step',) + (", 1)  # the module as it would be with a step nobody declared
    with pytest.raises(RuntimeError, match="a_new_step"):
        exec(compile(src, "sellchain_with_a_new_step", "exec"), {"__name__": "sellchain_with_a_new_step"})


def test_a_bet_bought_late_still_has_its_early_profit_zec_2230():
    """ZEC 22:30 NO, 2026-11-26: bought 0.67 at minute 6, 9c ahead at minute 9, then to zero. The fade counts the bet's own age, so a late buy is not born with its early profit already faded. User: "those should have been Profits And rebuys"."""
    ask = asker(0.67, wobble=0.040, bought=361.0)  # bought with 539 s left
    assert not ask(0.70, 400, 5.0).sell
    assert not ask(0.79, 369, 5.0).sell  # 22:23:51: +9c after both fees, at minute 8.9 of the market and 170 s into the bet
    r = ask(0.73, 364, 5.0)  # 22:23:56 on the tape: fell 0.06
    assert r.sell and r.kind == "fall" and r.net > 0 and r.fade > 0.98
    # and it fades quicker the less time the bet had (user: "it should fade quicker based o the time left"): a tenth two thirds of the way through the bet's own life
    assert 0.05 < fallrule.fade(600.0, 900.0) < 0.2 and 0.05 < fallrule.fade(360.0, 540.0) < 0.2 and 0.05 < fallrule.fade(120.0, 180.0) < 0.2
    assert fallrule.fade(360.0, 900.0) > 0.95 > 0.2 > fallrule.fade(360.0, 540.0)  # six minutes in: fresh for a bet bought at the open, nearly gone for one bought with nine minutes left
    late = asker(0.67, wobble=0.040, bought=361.0)
    late(0.70, 400, 5.0), late(0.90, 150, 5.0)  # the same bet 6.5 minutes into its 9: a 9c fall is left alone
    assert not late(0.81, 145, 5.0).sell
    old = asker(0.67, wobble=0.040)  # the same bids on a bet as old as the market: left alone, as the rule did that night
    old(0.70, 400, 5.0), old(0.79, 369, 5.0)
    assert not old(0.73, 364, 5.0).sell


def test_a_small_profit_is_not_sold_for_pennies_on_one_wobble_doge_2330():
    """DOGE 23:30 YES, 2026-11-26: bought 0.66, high 0.76, sold at 0.69 for +2.0c on a one-wobble fall while the spot was on the bet's side; the bid went on to 0.80. User: "for garbage profit, while it was winning. Twice". A profit run starts at two wobbles, so a sale always keeps one."""
    ask = asker(0.66, wobble=0.045, bought=473.0)  # bought 23:22:49, 427 s left
    assert not ask(0.73, 380, 3.0).sell
    assert ask(0.76, 370, 3.0).kind == "nothing"  # +7c after both fees, 1.6 wobbles: not a profit run
    assert not ask(0.71, 359, 3.0).sell  # 23:24:01, the sale that night: left alone
    assert not ask(0.80, 330, 3.0).sell  # +11c: the run starts here
    r = ask(0.73, 300, 3.0)  # a fall that still keeps a wobble of profit is taken
    assert r.sell and r.kind == "fall" and r.net >= 0.04  # about a wobble kept (the sale fee is a little higher at the lower bid)


def test_a_profit_gained_too_fast_is_taken_even_with_a_cushion_near_0000():
    """NEAR 00:00 NO, 2026-11-27: bought 0.64 with 519 s left, 0.83 a hundred and thirteen seconds later, held by the cushion, then zero. User: "We need a rule where the time to profit increase becomes too valuable to risk, and it needs to sell it". Pace = the share of the possible profit in hand over the share of the bet's life used; 2 or more sells."""
    ask = asker(0.64, wobble=0.040, bought=381.0)  # bought with 519 s left
    assert not ask(0.70, 480, 40.0).sell  # +4c: under two wobbles, no profit run, so no pace is read
    r = ask(0.83, 406, 40.0)  # 113 s in (22% of its life) with 47% of the possible profit in hand: pace 2.2, with the spot a sigma on its side
    assert r.sell and r.kind == "fast" and r.net > 0.15
    slow = asker(0.64, wobble=0.040, bought=381.0)
    assert not slow(0.76, 300, 40.0).sell  # the same bet 219 s in with 26% in hand: pace 0.6
    assert slow(0.83, 150, 40.0).kind == "cushion"  # 47% in hand at 71% of its life: pace 0.7, an ordinary winner, left to the rest of the rule


def test_integration_a_fast_profit_sale_goes_through_the_selling_thread_to_an_order(tmp_path, monkeypatch):
    from kalshi_moonshot import watcher
    from tests.test_risk_exit_flow import TICKER, Client, held, live, signals, yes_bid

    live(monkeypatch)
    client = Client(bid=0.95, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['one_rule']")
    t = w.targets[(TICKER, "risk_only")]
    t.shock_index, t.shock_ref = "X", 100.0
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: (40.0, 10.0))  # a cushion that would hold a fall
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.10)
    yes_bid(w, 0.95, 440)  # bought at about 0.79 ten seconds before: most of the possible profit already in hand
    got = signals(w)
    assert got and got[0].why.startswith("one rule, profit gained too fast to risk:"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert [n for n, _ in client.sold] == [4.65]
