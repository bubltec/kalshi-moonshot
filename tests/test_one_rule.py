"""The one exit rule (fallrule.py; user, 2026-11-26: "we need to consolidate one rule that handles them all"): early profit that fades with the time elapsed, no loss while the spot has not crossed, salvage on a failed bounce, the time backstop and the top. Unit tests on the pure function, then the watcher's step through a held bet."""
import itertools
import math

import pytest

from kalshi_moonshot import fallrule, memguard
from kalshi_moonshot.strategy import fee_per_contract as fee


def asker(entry, wobble=0.043, spot_unit=10.0, bought=1e-9):
    """`bought` is the bet's buy time on a clock where the market opens at 0 and closes at 900 (the default: bought at the open, so the bet's age is the market's clock)."""
    s = fallrule.State()

    def ask(bid, left, cushion=None, cross=None, fast=None):
        return fallrule.decide(s, bid, entry, fee(bid), fee(entry), fee, wobble, 900.0 - left, left, cushion, spot_unit if cushion is not None else None, cross, None, bought, fast_cc=fast)

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
    fades = [fallrule.fade(60.0 * m) for m in range(15)]  # by the bet's age in minutes
    assert all(a >= b for a, b in itertools.pairwise(fades))  # it only ever weakens
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
    assert not ask(0.57, 285, -30.0).sell  # the collapse, spot crossed: not sold on the way down
    assert not ask(0.15, 273, -30.0).sell  # the low
    assert not ask(0.30, 250, -30.0).sell  # bounced a bar off the low
    r = ask(0.20, 235, -30.0)  # the bounce failed
    assert r.sell and r.kind == "salvage" and r.net < 0
    ask = asker(0.80, wobble=0.05)
    assert not ask(0.78, 760, -30.0).sell
    assert not ask(0.60, 600, -30.0).sell  # sliding with no bounce
    r = ask(0.40, 460, -30.0)  # 300 s under cost since first seen, spot crossed
    assert r.sell and r.kind == "no recovery"


def test_the_cross_reads_the_better_of_two_cushions_and_the_top_sells():
    ask = asker(0.78, wobble=0.05)
    ask(0.15, 273, -30.0)
    ask(0.30, 250, -30.0)
    assert not ask(0.20, 235, -30.0, cross=1.0).sell  # the 5 s average is back over the target: not crossed
    assert ask(0.20, 234, -30.0, cross=-30.0).sell
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
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: (-30.0, 10.0))  # the spot is two wobbles of its own on the wrong side
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
    assert (got[0].ticker, got[0].lane) not in w.targets  # the sold bet's target is gone at once: a re-buy seconds later is watched from a fresh one, not on this bet's price and memory (SOL 04:00, 2026-11-27)
    from kalshi_moonshot import config
    from kalshi_moonshot.store import Store

    assert len(Store(config.load().db_path).open_bets()) == 0  # and the bet is closed


def test_every_sell_step_declares_whether_it_may_sell_under_cost_and_the_selling_thread_reads_only_that():
    """User, 2026-11-26: "Make a note when adding rules that it must be in this list or set up a coding mechanism to make this automatic". A step with no entry stops the import; the selling thread's list is the table."""
    import inspect

    import pytest

    from kalshi_moonshot import sellchain, watcher

    assert set(sellchain.LOSS_REASONS) == set(sellchain.SELL_STEPS)
    assert sellchain.LOSS_PREFIXES == ("peak-crash", "stop-loss", "curve risk exit", "wave give-up", "value sale", "late dip", "no recovery", "one rule", "expiry sale")  # the list as it stood, in the same order, now built from the table (expiry sale added 2026-11-28)
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


def test_a_profit_gained_too_fast_is_taken_even_with_a_cushion_near_0000(monkeypatch):
    monkeypatch.setattr(fallrule, "MARKET_FADE_FROM", 2.0)  # these tests are about the fast clause itself, not the end-of-market hold
    """NEAR 00:00 NO, 2026-11-27: bought 0.64 with 519 s left, 0.83 a hundred and thirteen seconds later, held by the cushion, then zero. User: "We need a rule where the time to profit increase becomes too valuable to risk, and it needs to sell it". Pace = the share of the possible profit in hand over the share of the bet's life used; 2 or more sells."""
    ask = asker(0.64, wobble=0.040, bought=381.0)  # bought with 519 s left
    assert not ask(0.70, 480, 40.0).sell  # +4c: under two wobbles, no profit run, so no pace is read
    assert not ask(0.83, 406, 40.0).sell  # 113 s in (22% of its life) with 47% of the possible profit in hand: pace 2.2, but still at its high: held while it climbs (SOL 01:15: "It shouldn't sell if it doesn't stop")
    assert not ask(0.88, 380, 40.0).sell  # a new high
    assert not ask(0.84, 370, 40.0).sell  # a wobble off the high but only 10 s without a new high (the heartbeat is 5% of the 370 s left, 18 s): not stopped
    assert not ask(0.89, 366, 40.0).sell  # a new high: the beat starts again
    r = ask(0.88, 345, 40.0)  # 21 s without a new high, over the 17 s beat: it has stopped, sold
    assert r.sell and r.kind == "fast" and r.net > 0.15
    flat = asker(0.64, wobble=0.040, bought=381.0)
    flat(0.70, 480, 40.0), flat(0.83, 406, 40.0)
    assert not flat(0.82, 390, 40.0).sell  # sixteen seconds flat: not a stall yet
    r2 = flat(0.82, 360, 40.0)  # no new high for 46 s, over the 18 s beat
    assert r2.sell and r2.kind == "fast"
    sol = asker(0.73, wobble=0.045, bought=697.0)  # SOL 01:15: bought with 203 s left, 0.91 thirty-six seconds later and still rising
    assert not sol(0.83, 190, 5.0).sell and not sol(0.91, 167, 5.0).sell and not sol(0.96, 150, 5.0).sell
    assert sol(0.99, 110, 5.0).kind == "top"
    slow = asker(0.64, wobble=0.040, bought=381.0)
    assert not slow(0.76, 300, 40.0).sell  # the same bet 219 s in with 26% in hand: pace 0.6
    assert slow(0.83, 150, 40.0).kind == "cushion"  # 47% in hand at 71% of its life: pace 0.7, an ordinary winner, left to the rest of the rule


def test_integration_a_fast_profit_sale_goes_through_the_selling_thread_to_an_order(tmp_path, monkeypatch):
    monkeypatch.setattr(fallrule, "MARKET_FADE_FROM", 2.0)  # the fast clause itself, not the end-of-market hold
    from kalshi_moonshot import watcher
    from tests.test_risk_exit_flow import TICKER, Client, held, live, signals, yes_bid

    live(monkeypatch)
    client = Client(bid=0.90, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['one_rule']")
    t = w.targets[(TICKER, "risk_only")]
    t.shock_index, t.shock_ref = "X", 100.0
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: (40.0, 10.0))  # a cushion that would hold a fall
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.10)
    yes_bid(w, 0.95, 440)  # bought at about 0.79 ten seconds before: most of the possible profit already in hand, still at its high
    assert signals(w) == []  # held while it climbs
    yes_bid(w, 0.90, 437)  # a wobble off the high, but only 3 s without a new high: not stopped (the heartbeat is 5% of the time left, about 22 s)
    assert signals(w) == []
    yes_bid(w, 0.92, 410)  # 30 s without a new high: it has stopped
    got = signals(w)
    assert got and got[0].why.startswith("one rule, profit gained too fast to risk:"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert [n for n, _ in client.sold] == [4.65]


def test_a_loss_is_sold_only_when_the_spot_is_as_far_under_the_target_as_it_has_been_over_it():
    """BNB 00:45 YES, 2026-11-27: the spot had been about $1.00 over the target, went $0.20 to $0.33 under for three minutes, the salvage sold at 0.32 and the market settled YES. ETH 00:45 YES: best $1.76, then $1.77 under and falling. User: "compared to how far the cushion was above earlier in the market it should allow a wait before selling to see if it comes back up"; "It needs to be the entire market"."""
    def bounce_fail(ask, cushion):
        ask(0.40, 300, cushion), ask(0.26, 280, cushion), ask(0.38, 260, cushion)  # a collapse, a low, a bounce of more than the bar
        return ask(0.30, 244, cushion)  # the bounce fails

    bnb = asker(0.84, wobble=0.05, spot_unit=0.44)  # a wobble of the spot is 0.18
    assert not bnb(0.85, 560, 1.00).sell  # the spot $1.00 over the target: the best cushion of the market so far
    r = bounce_fail(bnb, -0.25)  # $0.25 under: past a wobble, a quarter of what it had been over
    assert not r.sell and r.kind == "no cross"
    eth = asker(0.84, wobble=0.05, spot_unit=0.44)
    assert not eth(0.85, 560, 1.00).sell
    r = bounce_fail(eth, -1.10)  # further under than it was ever over: crossed
    assert r.sell and r.kind == "salvage"
    seeded = asker(0.84, wobble=0.05, spot_unit=0.44)
    seeded.state.best_cushion = 1.00  # the best came BEFORE the buy (the watcher seeds it from the market's open): the same answer
    assert bounce_fail(seeded, -0.25).kind == "no cross"
    none = asker(0.84, wobble=0.05, spot_unit=0.44)  # a market whose spot was never on the bet's side: a wobble under is a cross, as before
    assert bounce_fail(none, -1.0).kind == "salvage"  # a full typical move of the time left under the target (the salvage's gate, SALVAGE_Z)


def test_integration_the_watcher_seeds_the_best_cushion_from_the_markets_open(tmp_path, monkeypatch):
    from kalshi_moonshot import ticks, watcher
    from tests.test_risk_exit_flow import CLOSE, TICKER, held

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['one_rule']")
    t = w.targets[(TICKER, "risk_only")]
    t.shock_index, t.shock_ref, t.side = "SEED", 100.0, "yes"
    ticks.history.clear()
    for k in range(460):  # from the open: 100 s at 101.5 (a cushion of 1.5 for YES, long before the buy), then back to 100.2
        ticks.history.note("SEED", CLOSE - 900.0 + k, 101.5 if k < 100 else 100.2)
    try:
        best = watcher.ProfitWatcher._best_cushion_since_open(w, t, CLOSE - 440.0)
        assert best == pytest.approx(1.5, abs=0.01)
        t.side = "no"
        assert watcher.ProfitWatcher._best_cushion_since_open(w, t, CLOSE - 440.0) < 0  # never on the NO side: nothing to compare a dip with
    finally:
        ticks.history.clear()


def test_the_top_holds_at_any_minute_when_the_cushion_makes_holding_worth_more_than_the_bid():
    """BTC 01:15 YES, 2026-11-27: sold at 0.991 with 164 s left, outside the last minute the locked-in hold covers. User: "this also sold again at 99c, didn't we get it to stop this?". The same test on the plain cushion: about 2.3 sigma of the time left at 0.99."""
    ask = asker(0.64, spot_unit=18.0)
    far = ask(0.991, 164, 80.0)  # $80 over the target with 164 s left: 2.7 sigma
    assert not far.sell and far.kind == "locked"
    near = asker(0.64, spot_unit=18.0)(0.991, 164, 50.0)  # $50 over: 1.7 sigma, not enough to be worth more than 0.99
    assert near.sell and near.kind == "top"
    assert asker(0.64, spot_unit=18.0)(0.991, 164).kind == "top"  # the spot unread: sold as before


def test_a_straight_fast_drop_toward_the_target_late_in_the_market_is_sold_for_a_small_loss(monkeypatch):
    monkeypatch.setattr(fallrule, "FAST_DROP_ON", True)
    """User, 2026-11-27 11:50 ET: "it should also salvage if we get a fast drop that approaches the target when time remaining is low. Fast drop happens when the slope away from winning side shows no/little curvature and approaches the target". ETH 08:00 NO, ZEC 08:00 NO, XRP/HYPE/BNB 10:15 NO fell under cost with the cushion still positive and were never sold. The spot's cushion falling in a straight line, with little time left, a small cushion for the time left, and reaching the target within half the time left."""
    ask = asker(0.70, wobble=0.045)
    sold = None
    for k in range(40):
        left = 170 - 2 * k
        r = ask(0.55, left, 7.0 - 0.2 * k * 2)  # the cushion falls 0.4 a tick, every 2 s: a straight line
        if r.sell:
            sold = (k, r)
            break
    assert sold is not None and sold[1].kind == "fast drop" and sold[1].net < 0, sold
    assert 8 <= sold[0] <= 20  # once the window has filled, not on the first tick
    # a fall that slows (curvature) is not a straight one: the same cushion path flattening out is held
    ask = asker(0.70, wobble=0.045)
    path = [1.0 + 6.0 * math.exp(-k / 6.0) for k in range(40)]  # a fall that decays: it flattens out well above the target
    assert not any(ask(0.55, 170 - 2 * k, c).sell for k, c in enumerate(path))
    # with a lot of time left it is not read at all
    ask = asker(0.70, wobble=0.045)
    assert not any(ask(0.55, 500 - 2 * k, 7.0 - 0.4 * k).sell for k in range(30))
    # nor with a cushion that is still large for the time left
    ask = asker(0.70, wobble=0.045)
    assert not any(ask(0.55, 170 - 2 * k, 60.0 - 0.4 * k).sell for k in range(40))


def test_the_fast_early_take_is_inert_in_the_last_tenth_of_the_market(monkeypatch):
    monkeypatch.setattr(fallrule, "MARKET_FADE_FROM", 2.0)  # these tests are about the fast clause itself, not the end-of-market hold
    """User, 2026-11-27 11:50 ET: the fast early take "waits longer with less time remaining, with about 90s left making it inert"."""
    ask = asker(0.64, wobble=0.040, bought=700.0)  # bought with 200 s left
    ask(0.70, 195, 40.0)
    assert not ask(0.84, 170, 40.0).sell  # 30 s in, most of the profit in hand: a fast profit
    assert ask(0.84, 150, 40.0).kind == "fast"  # 20 s without a new high with 150 s left: sold
    ask = asker(0.64, wobble=0.040, bought=700.0)
    ask(0.70, 195, 40.0)
    ask(0.84, 170, 40.0)
    assert ask(0.84, 85, 40.0).kind != "fast"  # the same bid with 85 s left: the fast clause is inert, the ordinary early take decides


def test_integration_a_fast_drop_sale_goes_through_the_selling_thread_to_an_order(tmp_path, monkeypatch):
    monkeypatch.setattr(fallrule, "FAST_DROP_ON", True)
    """The fast drop is a sale under cost, so its reason must pass sellchain.may_sell_under_cost and the selling thread must place it (the 11-26 lesson: a step is tested through to the client's order)."""
    from kalshi_moonshot import config, watcher
    from kalshi_moonshot.store import Store
    from tests.test_risk_exit_flow import CLOSE, Client, held, live, signals, yes_bid

    live(monkeypatch)
    client = Client(bid=0.30, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['one_rule']")
    t = w.targets[("KXFAKE", "risk_only")] if ("KXFAKE", "risk_only") in w.targets else next(iter(w.targets.values()))
    t.shock_index, t.shock_ref = "X", 100.0
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: (7.0 - 0.2 * (170 - (CLOSE - now)), 10.0))  # the cushion falls 0.2 a second, in a straight line
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.12)
    got = []
    for k in range(40):
        yes_bid(w, 0.45, 170 - 2 * k)
        got += signals(w)
        if got:
            break
    assert got and got[0].why.startswith("one rule, fast drop:"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert len(client.sold) == 1, "the selling thread did not place the fast drop's sale under cost"
    assert len(Store(config.load().db_path).open_bets()) == 0


def test_early_take_holds_a_fall_while_the_cushion_approaches_the_target_with_curvature_and_sells_it_when_the_approach_is_straight():
    """User, 2026-11-27 12:15 ET: normal early take "should hold as long as it has a curvature while approaching the target which indicates it will likely hold on the winning side. The other exits should handle the case where it doesn't". The cushion is small for the time left in both runs (under a sigma), and the bid falls the same way."""

    def run(path):
        ask = asker(0.64, wobble=0.040, bought=1e-9)
        out = None
        for k, c in enumerate(path):
            out = ask(0.80, 600 - 5 * k, c)  # a profit run at 0.80 while the cushion moves
        return ask, out

    straight = [9.0 - 0.3 * k for k in range(15)]  # falls in a straight line toward the target
    ask, _ = run(straight)
    r = ask(0.69, 600 - 5 * 15, straight[-1] - 0.3)
    assert r.sell and r.kind == "fall", r
    curved = [4.5 + 4.5 * math.exp(-k / 4.0) for k in range(15)]  # falls but slows: it flattens above the target
    ask, _ = run(curved)
    r = ask(0.69, 600 - 5 * 15, curved[-1] - 0.01)
    assert not r.sell and r.kind == "cushion", r
    rising = [4.5 + 0.2 * k for k in range(15)]  # moving away from the target: no longer a hold of its own (XRP 22:00 YES, 2026-11-27: a +17c profit held through falls of 11 to 15c by a rising cushion, then to zero)
    ask, _ = run(rising)
    r = ask(0.69, 600 - 5 * 15, rising[-1])
    assert r.sell and r.kind == "fall", r


def test_the_fast_drop_is_switched_off():
    """User, 2026-11-27 19:55 ET, "Yes turn it off": five live sales were about -$45.4 against holding. A straight fast fall of the cushion near the close no longer sells."""
    assert fallrule.FAST_DROP_ON is False
    ask = asker(0.70, wobble=0.045)
    assert not any(ask(0.45, 170 - 2 * k, 7.0 - 0.4 * k).sell for k in range(14))


def test_salvage_depth_is_scaled_by_the_time_left_and_inert_in_the_last_minutes():
    """User, 2026-11-28 ~09:30 ET: "The salvage rule needs to stop waiting a typical minute on the last minutes that's stupid. The time scalar should make that minute rule inert." The failed bounce is sold only once the spot's cushion is under SALVAGE_Z (-1) typical moves of the time left TIMES the share of the market left (and the cross needs the best cushion times it): deep when the market is young, nothing in its last minutes. BTC 21:00 NO (369 s left, -0.53 under, settled NO) and BTC 06:15 NO (the last three minutes, never sold) are the two sides."""
    def run(cushion, left_at):
        ask = asker(0.58, wobble=0.05, spot_unit=10.0)
        ask(0.40, left_at + 51, cushion), ask(0.26, left_at + 31, cushion), ask(0.38, left_at + 11, cushion)  # a collapse, a low, a bounce of more than the bar
        return ask(0.30, left_at, cushion)  # the bounce fails

    young = run(-13.0, 700)  # -13 against 10 x sqrt(700/60) = 34: -0.38 of a typical move, the gate -0.78 at 700 s left
    assert not young.sell, young
    young_deep = run(-40.0, 700)  # -1.2 of a typical move
    assert young_deep.sell and young_deep.kind == "salvage", young_deep
    last = run(-6.0, 120)  # -6 against 10 x sqrt(120/60) = 14: -0.42 of a typical move, the gate -0.13 at 120 s left
    assert last.sell and last.kind == "salvage", last



def test_integration_the_uptick_take_follows_a_rising_bid_and_sells_when_it_pauses_through_the_selling_thread_to_an_order(tmp_path, monkeypatch):
    """BTC 22:30 YES, 2026-11-27 (user: "It's literally just buy like it did in this example and sell on the uptick"; "The only reason it should hold is if it keeps profiting. But it started flattening out and should have sold"): the wave lane's early_take, with a pause of 0.5% of the time left and no cushion hold. The bid climbs from the cost: held while it makes new highs, sold at the bid the moment it pauses, at a gain, as an order."""
    from kalshi_moonshot import watcher
    from tests.test_risk_exit_flow import Client, held, live, signals, yes_bid

    live(monkeypatch)
    client = Client(bid=0.90, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['early_take']\nearly_take_share=1.0\nearly_take_stall_share=0.005\nearly_take_cushion_units=0.0\nearly_take_hold_share=0.0")
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: None)
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.10)  # the bid's typical minute: a 10 s wobble is about 4c

    class Board:
        def view(self, name):
            return {"p_lose": 0.9}  # the spot's chance of ending on the wrong side: a wave bet is bought cheap because the spot is on the wrong side, so a stall is sold, not held

    monkeypatch.setattr(watcher.ProfitWatcher, "_board", lambda self, c, tt, now, left, bid: Board())
    yes_bid(w, 0.83, 440)  # bought at 0.81: +2c, under a wobble after both fees: not a profit yet
    assert signals(w) == []
    for k, bid in enumerate((0.86, 0.88, 0.90, 0.92)):
        yes_bid(w, bid, 439 - k)  # a profit that keeps climbing: a new high every second
        assert signals(w) == [], f"sold while still climbing at {bid}"
    yes_bid(w, 0.92, 434)  # no new high for 1 s: the pause is 0.5% of 434 s, about 2 s
    assert signals(w) == []
    yes_bid(w, 0.92, 431)  # 4 s without a new high: it has flattened
    got = signals(w)
    assert got and got[0].why.startswith("early take"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert [n for n, _ in client.sold] == [4.65]


def test_integration_the_expiry_sale_sells_a_dying_bet_in_the_last_share_of_the_market_through_the_selling_thread_to_an_order(tmp_path, monkeypatch):
    """DOGE 07:15 YES, 2026-11-28 (user: "In the doge one we allowed it to go to settlement which is unacceptable. We need to sell before the exit so it's not a total loss"): bought at 0.15 with 61 s left, the bid went to nothing, no sale step armed. The expiry sale sells at the bid in the last 5% of the market (45 s), a loss included, and holds a near-sure win."""
    from kalshi_moonshot import sellchain, watcher
    from tests.test_risk_exit_flow import Client, held, live, signals, yes_bid

    assert "expiry_sale" in sellchain.SELL_STEPS and sellchain.may_sell_under_cost("expiry sale: 40 s left")
    live(monkeypatch)
    client = Client(bid=0.08, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['expiry_sale']\nexpiry_sale_share=0.05\nexpiry_sale_max_bid=0.90")
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: None)
    yes_bid(w, 0.20, 100)  # 100 s left: not yet
    assert signals(w) == []
    yes_bid(w, 0.0, 44)  # no bid at all: nothing to sell to
    assert signals(w) == []
    yes_bid(w, 0.08, 43)  # inside the last 45 s: the bet is dying, sold at the bid
    got = signals(w)
    assert got and got[0].why.startswith("expiry sale"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert [n for n, _ in client.sold] == [4.65], "the selling thread did not place the expiry sale"
    w2 = held(tmp_path, monkeypatch, live=True, client=Client(bid=0.95, account=None), extra="sell_chain=['expiry_sale']\nexpiry_sale_share=0.05\nexpiry_sale_max_bid=0.90")
    yes_bid(w2, 0.95, 30)  # a near-sure win is held to settlement
    assert signals(w2) == []


def test_a_favourite_held_into_the_end_of_the_market_fades_the_profit_rules_on_the_market_clock():
    """User, 2026-11-28, on XRP 08:15 NO (sold at 0.976 on the fast clause, the market ran to 1.00): "Under 500s remaining if our held buy is in the 80-90s we should not sell and let our salvage handle issues where the market crashes out", and it "should be built into two existing rules": early profit's fade, read on the market's elapsed time as well as the bet's age, for a bid at 0.80 or more. Early in the market the same fall sells, late it is held, a bid under 0.80 is not faded, and the top still sells."""
    def run(left_at, bid_after):
        ask = asker(0.70, wobble=0.040, bought=900.0 - left_at - 40.0)  # bought 40 s before the first look: a young bet, nothing faded by its own age
        ask(0.78, left_at + 20, 5.0)
        ask(0.93, left_at + 10, 5.0)  # a profit run to 0.93 (a fast one)
        return ask(bid_after, left_at, 5.0)

    early = run(700, 0.80)  # a fall of 13c with 700 s left
    assert early.sell and early.kind in ("fall", "fast"), early
    late = run(250, 0.80)  # the same fall with 250 s left, the bid in the 80s: faded away
    assert not late.sell, late
    top = run(250, 0.995)
    assert top.sell and top.kind == "top"
    low = run(250, 0.76)  # under 0.80: the market clock does not fade it
    assert low.sell, low


def test_integration_the_early_take_with_bid_only_sells_a_stalled_bid_while_the_spot_still_climbs(tmp_path, monkeypatch):
    """ETH 09:45 YES, 2026-11-28 (user: "The price went up to about 55 and stabilized and then it didn't sell. Why? That's what it is supposed to do"): the bid stalled and fell back while the spot's cushion kept climbing, and the step counted the climbing spot as 'not stalled'. With early_take_bid_only the bid alone decides."""
    from kalshi_moonshot import watcher
    from tests.test_risk_exit_flow import Client, held, live, signals, yes_bid

    def build(extra_bid_only):
        live(monkeypatch)
        client = Client(bid=0.90, account=None)
        w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['early_take']\nearly_take_share=1.0\nearly_take_stall_share=0.005\nearly_take_cushion_units=0.0\nearly_take_hold_share=0.0" + ("\nearly_take_bid_only=true" if extra_bid_only else ""))
        monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.10)
        cush = [10.0]

        def spot(self, tt, now, avg_seconds=60):
            cush[0] += 2.0  # the spot's cushion makes a new high on every look: well over a wobble of its own
            return (cush[0], 3.0)

        monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", spot)

        class Board:
            def view(self, name):
                return {"p_lose": 0.9}

        monkeypatch.setattr(watcher.ProfitWatcher, "_board", lambda self, c, tt, now, left, bid: Board())
        return w, client

    w, _client = build(False)
    for k, bid in enumerate((0.83, 0.86, 0.88, 0.90, 0.90, 0.90, 0.90)):
        yes_bid(w, bid, 440 - k)
    assert signals(w) == []  # the climbing spot keeps the bet 'not stalled': held while the bid sits at its high
    w2, client2 = build(True)
    for k, bid in enumerate((0.83, 0.86, 0.88, 0.90)):
        yes_bid(w2, bid, 440 - k)
        assert signals(w2) == []
    for k in range(3):
        yes_bid(w2, 0.90, 436 - k)
    got = signals(w2)
    assert got and got[0].why.startswith("early take"), got
    w2._sell(got[0].ticker, got[0].lane, got[0].why)
    assert client2.sold and client2.sold[0][0] > 0, "the selling thread did not place the sale"  # (the two builds share one store here, so the bet is held twice: 9.3 contracts)


def test_integration_a_bid_only_uptick_take_sells_a_fall_late_in_the_first_half_whatever_the_boards_hold_value_says(tmp_path, monkeypatch):
    """XRP 12:45 YES, 2026-11-28 (user: "The problem is it never exited"): bought 0.31 at 8 minutes in, the bid went to 0.47 and slid back to 0.40 over 15 s with 355 s left, past the half of the market, where the hold-value check (a favourite's check: the board says the bet is likely to win) and the winning-side hold kept it; it went to zero. A scalp lane reads the bid alone."""
    from kalshi_moonshot import watcher
    from tests.test_risk_exit_flow import Client, held, live, signals, yes_bid

    live(monkeypatch)
    client = Client(bid=0.90, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['early_take']\nearly_take_share=1.0\nearly_take_stall_share=0.005\nearly_take_cushion_units=0.0\nearly_take_hold_share=0.0\nearly_take_bid_only=true")
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.10)

    class Board:
        def view(self, name):
            return {"p_lose": 0.05}  # the board thinks the hold is nearly safe

    monkeypatch.setattr(watcher.ProfitWatcher, "_board", lambda self, c, tt, now, left, bid: Board())
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: (5.0, 3.0))  # the spot reads as on the winning side
    path = [0.84, 0.88, 0.93, 0.95, 0.94, 0.92, 0.91, 0.90, 0.89, 0.88, 0.88]  # the fixture's bet is bought at 0.81: the same shape as XRP 0.31 -> 0.47 -> 0.40, shifted up
    for k, bid in enumerate(path):
        yes_bid(w, bid, 370 - k)
    got = signals(w)
    assert got and got[0].why.startswith("early take"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert client.sold and client.sold[0][0] > 0


def test_the_last_90_seconds_read_the_spots_own_cushion_and_the_salvage_stops_waiting_for_a_bounce_eth_1000_no():
    """ETH 10:00 NO, 2026-11-28: bought 0.85, held at 0.82 to 0.92 into the last 30 s with the spot's cushion small; the spot then climbed over the target (5 s cushion -0.24, -0.59) and the NO bid went 0.88 -> 0.38 -> 0.27 -> 0.19 and the bet lost the stake by a cent. The cross read the better of the 5 s and the 60 s average (still on the winning side) and asked a wobble that did not shrink with the clock, and the salvage waited for a bounce that never came. User: "It cannot block on the 60s ... if there's a slope towards the losing side and time is running out we need to exit"; "That last slope up looked like it might hold. But it didn't, and that's what salvage needs to handle"."""
    ask = asker(0.85, wobble=0.04, spot_unit=0.98)
    assert not ask(0.90, 39, 0.27, fast=0.20).sell and not ask(0.88, 30, 0.24, fast=0.09).sell  # a bet ahead with the spot on its side: held
    r = ask(0.38, 27, 0.20, fast=-0.24)  # the spot is over the target on its own 5 s reading with no bounce of a bar: the last 90 s do not wait for one
    assert r.sell and r.kind == "salvage" and r.net < 0, r
    # the same bid path with the late reading off holds to the end (the 60 s average never crossed)
    old = asker(0.85, wobble=0.04, spot_unit=0.98)
    for left, bid, c, f in ((39, 0.90, 0.27, 0.20), (30, 0.88, 0.24, 0.09), (27, 0.38, 0.20, -0.24), (24, 0.27, 0.15, -0.59)):
        fallrule.LATE_CROSS_ON = False
        try:
            assert not old(bid, left, c, fast=f).sell
        finally:
            fallrule.LATE_CROSS_ON = True
    # and outside the last 90 s the cross is still the 60 s reading and the wobble is not scaled
    early = asker(0.85, wobble=0.04, spot_unit=0.98)
    assert not early(0.40, 300, 0.20, fast=-0.59).sell and not early(0.27, 296, 0.15, fast=-0.59).sell


def test_integration_the_late_cross_and_scaled_salvage_sale_goes_through_the_selling_thread_to_an_order(tmp_path, monkeypatch):
    """The late salvage is a sale under cost: its reason opens "one rule" and the selling thread places it (the 11-26 lesson), here with the 60 s cushion still on the winning side and the 5 s cushion over the target in the last 90 s."""
    from kalshi_moonshot import config, watcher
    from kalshi_moonshot.store import Store
    from tests.test_risk_exit_flow import Client, held, live, signals, yes_bid

    live(monkeypatch)
    client = Client(bid=0.30, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=client, extra="sell_chain=['one_rule']")
    t = next(iter(w.targets.values()))
    t.shock_index, t.shock_ref = "X", 100.0
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: ((2.0 if avg_seconds != 5 else -5.0), 10.0))  # the 60 s average still on the winning side, the 5 s average over the target
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit", lambda self, tt, now: 0.12)
    got = []
    for k in range(40):
        yes_bid(w, 0.30, 60 - 2 * k)
        got += signals(w)
        if got:
            break
    assert got and got[0].why.startswith("one rule, salvage:"), got
    w._sell(got[0].ticker, got[0].lane, got[0].why)
    assert len(client.sold) == 1, "the selling thread did not place the late salvage's sale under cost"
    assert len(Store(config.load().db_path).open_bets()) == 0


def test_two_typical_minutes_of_cushion_holds_a_fall_at_485_s_left_eth_1330_yes():
    """ETH 13:30 YES, 2026-11-28: bought 0.75, the bid ran to 0.88 and fell 0.08 with 485 s left and the spot's cushion at +0.97 sigma of the time left (2.8 typical minutes); early profit sold at 0.80 for +2.6c and the market settled YES. User: "We shouldn't sell that. Not with like 2x cushion". The hold is 0.7 sigma of the time left: two typical minutes at 485 s left."""
    def run(z_target):
        ask = asker(0.75, wobble=0.038, spot_unit=10.0, bought=247.0)  # bought 168 s before the sale, as live
        c = z_target * 10.0 * math.sqrt(485 / 60.0)
        for k, bid in enumerate((0.80, 0.84, 0.88)):
            ask(bid, 520 - 10 * k, c)
        return ask(0.80, 485, c)  # a fall of 0.08 from the 0.88 high

    held = run(0.97)
    assert not held.sell and held.kind == "cushion", held
    two_minutes = run(2.0 / math.sqrt(485 / 60.0))  # a cushion of two typical minutes
    assert not two_minutes.sell and two_minutes.kind == "cushion", two_minutes
    assert run(0.5).sell and run(0.5).kind == "fall"  # a cushion of 1.4 typical minutes: sold as before

