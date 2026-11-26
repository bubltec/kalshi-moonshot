"""The value_sale step (2026-11-23): a favourite is sold when the bid after the sale fee is at least what holding is worth (1 - p_lose, from the spot), plus the lane's value_sale_premium."""

from kalshi_moonshot import facts, memguard, sellchain
from kalshi_moonshot import ticks as ticks_mod
from tests.test_wave_top import INDEX, TOML, mem, spec, spot_feed, take_target  # noqa: F401


def asker(w, t, close):
    from kalshi_moonshot import curverisk, watcher

    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None  # each ask is a new check: a new board
        with memguard.decision("sell"):
            return w._sell_value_sale(t, risk, bid, close - left, left, 0.0, c)

    return ask


def test_the_step_is_known_and_reads_only_p_lose():
    sellchain.check(("value_sale",), "x")
    assert facts.NEEDS["value_sale"] == ("p_lose", "spot", "waves_before", "wave_now", "wave_leg")  # the wave facts (2026-11-25) are on the board for every step to read; value_sale does not act on them yet


def test_integration_it_sells_when_the_bid_pays_the_hold_value_and_not_before(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    ask = asker(w, t, close)
    spot_feed(close - 300, 100.3)  # the spot 0.3 over the target on the bet's side: p_lose about 0.19 with 5 minutes left, so holding is worth about 0.81
    assert ask(0.80, 300) is None  # pays 0.787
    why = ask(0.83, 300)  # pays 0.8165
    assert why and why.startswith("value sale:") and "hold value 0.80" in why and "premium of 0" in why and "[facts read:" in why and "p_lose=" in why


def test_integration_a_premium_holds_the_bets_the_market_does_not_overpay_for(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.value_sale_premium = 0.05
    ask = asker(w, t, close)
    spot_feed(close - 300, 100.3)
    assert ask(0.83, 300) is None  # pays 0.8165, the hold value 0.81 plus 0.05 is 0.86
    assert ask(0.88, 300) is not None  # pays 0.8688


def test_integration_the_spot_on_the_wrong_side_sells_at_a_loss(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    ask = asker(w, t, close)
    spot_feed(close - 300, 99.7)  # the spot 0.3 under the target: holding is worth about 0.19
    why = ask(0.50, 300)  # a bet that cost 0.80 sold at 0.50: the point is to get out
    assert why and why.startswith("value sale:")


def test_integration_no_spot_reading_holds(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    ticks_mod.history.clear()
    assert asker(w, t, close)(0.95, 300) is None


def test_integration_the_shadow_setting_never_sells(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.risk_live = False
    spot_feed(close - 300, 99.7)
    assert asker(w, t, close)(0.50, 300) is None


def test_the_lane_file_sets_the_premium(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.value_sale_premium == 0.0
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", "wave_top_trend_hours = 1.0\nvalue_sale_premium = 0.05\n"))
    from kalshi_moonshot import config, lanes

    assert lanes.lane_specs(config.load())["wt"].value_sale_premium == 0.05


def test_integration_value_hold_holds_a_favourite_the_market_underpays_for_and_only_after_the_flip(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.60, unit=0.10)
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left=300):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_value_hold(t, risk, bid, close - left, left, 0.0, c)

    spot_feed(close - 300, 100.3)  # after the flip, holding worth about 0.81
    assert ask(0.70) is sellchain.HOLD  # the bid pays 0.69: the market underpays
    assert ask(0.85) is None  # the bid pays 0.84: at or over the hold value, nothing to hold for
    t.value_sale_premium = 0.05
    assert ask(0.85) is sellchain.HOLD  # pays 0.84, under 0.81 + 0.05: held, as value_sale would not sell it either
    spot_feed(close - 300, 99.7)  # before the flip (the spot on the wrong side): not held
    assert ask(0.10) is None
    ticks_mod.history.clear()
    assert ask(0.10) is None  # no reading


def test_integration_value_after_flip_keeps_the_sale_from_acting_before_the_flip(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    spot_feed(close - 300, 99.7)
    assert asker(w, t, close)(0.50, 300) is not None  # off: it sells a bet the spot has on the wrong side
    t.value_after_flip = True
    assert asker(w, t, close)(0.50, 300) is None  # on: the bet is still a scalp
    spot_feed(close - 300, 100.3)
    assert asker(w, t, close)(0.85, 300) is not None  # after the flip it acts


def test_the_lane_file_sets_value_after_flip(tmp_path, monkeypatch):
    from kalshi_moonshot import config, lanes

    spec(tmp_path, monkeypatch)
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", "wave_top_trend_hours = 1.0\nvalue_after_flip = true\n"))
    assert lanes.lane_specs(config.load())["wt"].value_after_flip is True
    sellchain.check(("value_hold", "wave_take", "wave_salvage"), "x")


def test_integration_crossed_only_waits_for_the_spot_to_cross_the_target(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.value_crossed = True
    spot_feed(close - 300, 100.3)  # the spot still on the bet's side: not crossed
    assert asker(w, t, close)(0.30, 300) is None
    spot_feed(close - 300, 99.7)  # crossed
    assert asker(w, t, close)(0.50, 300) is not None


def test_integration_the_wait_is_a_share_of_the_time_left_and_a_break_restarts_it(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.value_wait_share = 0.25
    ask = asker(w, t, close)

    def at(bid, left):
        spot_feed(close - left, 99.7)  # the spot's ticks are fresh at every ask
        return ask(bid, left)

    assert at(0.50, 300) is None and t.value_since > 0  # the condition begins to hold: it must hold 0.25 x the time left
    assert at(0.50, 260) is None  # 40 s in, under 0.25 x 260 = 65 s
    assert at(0.50, 200) is not None  # 100 s in, over 0.25 x 200 = 50 s
    t.value_since = 0.0
    assert at(0.50, 300) is None
    assert at(0.05, 280) is None and t.value_since == 0.0  # the bid fell under the hold value: a break, the wait restarts
    assert at(0.50, 270) is None


def test_the_lane_file_sets_the_wait_and_the_cross(tmp_path, monkeypatch):
    from kalshi_moonshot import config, lanes

    spec(tmp_path, monkeypatch)
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", "wave_top_trend_hours = 1.0\nvalue_sale_crossed = true\nvalue_sale_wait_share = 0.25\n"))
    sp = lanes.lane_specs(config.load())["wt"]
    assert sp.value_sale_crossed is True and sp.value_sale_wait_share == 0.25


def test_integration_spot_perp_reads_the_perp_against_its_own_open(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.value_crossed = True
    now = close - 300
    spot_feed(now, 100.3)  # the settlement index: 0.3 over the target, the bet's side
    T = [close - 1500.0 + k for k in range(1501 - 300)]
    V = [100.3 if x <= close - 900.0 + 5 else 99.7 + 0.01 * (-1) ** int(x) for x in T]  # the perp: with the settlement index at the open (no basis), 99.7 now: under the 100.0 target
    ticks_mod.history.warm("KP_BTC", list(zip(T, V, strict=True)), T[0] - 1.0)
    assert asker(w, t, close)(0.50, 300) is None  # off: the settlement index says the spot is on the bet's side
    t.spot_perp = True
    assert asker(w, t, close)(0.50, 300) is not None  # on: the perp is under its open, the wrong side
    ticks_mod.history.clear()
    spot_feed(now, 100.3)
    t.perp_ref = 0.0
    assert asker(w, t, close)(0.50, 300) is None  # no perp reading: the settlement index again


def test_integration_spot_perp_measures_from_the_markets_own_target_not_the_perps_open(tmp_path, monkeypatch, mem):
    # DOGE 18:30 YES (2026-11-24): the perp read against its own value at the open gave +9 bp where the Kalshi strike gave +17 bp, and p_lose 0.16 sold a bet with a real cushion
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.spot_perp = True
    now = close - 300
    spot_feed(now, 100.5)  # the settlement index: 100.5 all through the window, the target (strike) is 100.0 (t.shock_ref)
    T = [close - 1500.0 + k for k in range(1501 - 300)]
    V = [100.5 + 0.01 * (-1) ** int(x) for x in T]  # the perp tracks it exactly: its own open is 100.5, 0.5 over the strike
    ticks_mod.history.warm("KP_BTC", list(zip(T, V, strict=True)), T[0] - 1.0)
    cushion = w._wave_spot(t, now)
    assert cushion is not None and cushion[0] > 0.45  # measured from the 100.0 strike, not from 100.5 (which would read 0)


def test_the_lane_file_sets_spot_perp(tmp_path, monkeypatch):
    from kalshi_moonshot import config, lanes

    spec(tmp_path, monkeypatch)
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", "wave_top_trend_hours = 1.0\nspot_perp = true\n"))
    assert lanes.lane_specs(config.load())["wt"].spot_perp is True


def test_integration_no_loss_share_bars_an_early_loss_sale_and_never_a_gain(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.no_loss_share = 0.5
    ask = asker(w, t, close)
    spot_feed(close - 800, 99.7)  # the spot on the wrong side, 800 s left: the first 100 s of the market
    assert ask(0.50, 800) is None  # a sale under the price paid in the first half: barred
    spot_feed(close - 300, 99.7)
    assert ask(0.50, 300) is not None  # past the first half the loss sale is open again
    spot_feed(close - 800, 100.5)
    assert ask(0.95, 800) is not None  # a gain early is never barred


def thin_cushion(monkeypatch, lose=0.3):
    from kalshi_moonshot import watcher

    class Board:
        def view(self, name):
            return {"p_lose": lose}

    monkeypatch.setattr(watcher.ProfitWatcher, "_board", lambda self, c, t, now, left, bid: Board())


def test_integration_early_take_follows_a_profit_and_sells_when_it_stops_climbing(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    thin_cushion(monkeypatch)  # a spot cushion that does not hold the bet: p_lose 0.3
    t.entry, t.early_take_share, t.early_take_stall_share = 0.80, 0.5, 0.0
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)
    now = [close - 800.0]

    def ask(bid, left):
        c.facts = None
        now[0] = close - left
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, now[0], left, 0.0, c)

    assert ask(0.78, 800) is None  # under the price paid: not armed, never a loss
    assert ask(0.84, 790) is None  # in profit by about 2c after fees, under one wobble (0.041): not armed
    assert ask(0.90, 780) is None  # over a wobble: armed, climbing
    why = ask(0.80, 770)  # fell from 0.90 but 0.80 pays under the price paid after fees: never a loss
    assert why is None
    t.unit_min_history = 0.0
    assert ask(0.93, 760) is None
    why = ask(0.88, 750)  # fell 5c from the 0.93 high, still in profit
    assert why and why.startswith("early take:") and "stopped climbing" in why
    t.early_take_armed = False
    assert ask(0.99, 300) is None  # past the first half of the market: not this step's


def test_integration_early_take_stall_sells_a_flat_profit(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    thin_cushion(monkeypatch)  # a spot cushion that does not hold the bet: p_lose 0.3
    t.entry, t.early_take_share, t.early_take_stall_share = 0.80, 0.5, 0.1
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.90, 800) is None  # armed at its high
    assert ask(0.90, 760) is None  # 40 s of no new high: under a tenth of the 760 s left (76 s)
    why = ask(0.90, 700)  # 100 s with no new high: over 70 s
    assert why and "no new high" in why


def test_integration_early_take_a_stall_sells_only_when_the_sale_beats_holding(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    class Board:
        lose = 0.005

        def view(self, name):
            return {"p_lose": Board.lose}

    w, t, close = take_target(tmp_path, monkeypatch, 0.70, unit=0.10)
    t.entry, t.early_take_share, t.early_take_stall_share = 0.70, 1.0, 0.1
    monkeypatch.setattr(watcher.ProfitWatcher, "_board", lambda self, c, t, now, left, bid: Board())
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.96, 600) is None  # armed at its high
    assert ask(0.96, 500) is None  # a flat bid for over a tenth of the time left, the spot's chance of a wrong ending 0.5%: holding is worth ~0.995, the sale pays under 0.96: hold
    Board.lose = 0.12  # the spot's cushion has gone: holding is worth 0.88, the sale pays more
    why = ask(0.96, 480)
    assert why and "no new high" in why


def _early_take_asker(tmp_path, monkeypatch, mem, stall_share, lose):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.70, unit=0.10)
    t.entry, t.early_take_share, t.early_take_stall_share, t.early_take_hold_share = 0.70, 1.0, stall_share, 0.0

    class Board:
        def view(self, name):
            return {"p_lose": lose[0]}

    monkeypatch.setattr(watcher.ProfitWatcher, "_board", lambda self, c, tt, now, left, bid: Board())
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, close - left, left, 0.0, c)

    ask.target = t
    return ask


def test_integration_early_take_a_stall_with_cushion_or_no_reading_holds(tmp_path, monkeypatch, mem):
    lose = [0.005]
    ask = _early_take_asker(tmp_path, monkeypatch, mem, 0.1, lose)  # falls are not asked: only the stall can sell here
    assert ask(0.90, 700) is None  # armed at its high
    assert ask(0.90, 600) is None  # no new high for 100 s (over 10% of the time left), but the spot's wrong-ending chance is 0.5%: holding is worth 0.995 against 0.89 from selling
    lose[0] = None
    assert ask(0.90, 590) is None  # no reading of the spot: nothing sells a stall on a number that is not there
    lose[0] = 0.3
    why = ask(0.90, 580)  # the cushion is gone: holding is worth 0.70 against 0.89
    assert why and "hold value" in why and "stopped climbing" in why


def test_integration_early_take_a_cushion_holds_a_fall_and_no_reading_sells_it(tmp_path, monkeypatch, mem):
    lose = [0.005]
    ask = _early_take_asker(tmp_path, monkeypatch, mem, 0.0, lose)
    assert ask(0.90, 700) is None  # armed at its high
    assert ask(0.80, 690) is None  # fell a wobble, +7c in hand, but a 0.5% chance of ending wrong: the hold is worth more than the sale (2026-11-25, SOL 07:34 NO: "with that much cushion it should not sell")
    lose[0] = None
    ask2 = _early_take_asker(tmp_path, monkeypatch, mem, 0.0, lose)
    assert ask2(0.90, 700) is None
    why = ask2(0.80, 690)  # no reading of the spot at all: nothing says hold, the fall sells (BNB 01:02 NO, HYPE 04:34 YES)
    assert why and "bid fell" in why and "hold value" not in why


def test_integration_no_recovery_sells_a_never_profitable_bet_after_half_a_market_since_the_buy(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.57, unit=0.10)
    t.entry, t.no_recovery_share, t.bought_ts = 0.57, 0.5, close - 800.0
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_no_recovery(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.55, 800) is None  # seen from the buy
    assert ask(0.40, 600) is None  # 200 s since the buy: an early loss is waited out
    assert ask(0.30, 400) is None  # 400 s: under half the market's length (450 s)
    why = ask(0.26, 340)  # 460 s since the buy, never ahead
    assert why and "no recovery" in why and "0.26" in why


def test_integration_no_recovery_waits_from_the_last_profit_and_sells_once_that_expires(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.57, unit=0.10)
    t.entry, t.no_recovery_share, t.bought_ts = 0.57, 0.5, close - 800.0
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_no_recovery(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.55, 800) is None
    assert ask(0.70, 700) is None  # in profit once: not this step's
    assert ask(0.30, 340) is None  # back under, but only 360 s since it was last ahead: still waiting
    why = ask(0.28, 240)  # 460 s since it was last ahead (left 700): the profit has expired
    assert why and "no recovery" in why and "last ahead" in why
    t2 = t
    t2.no_recovery_share = 0.0
    assert ask(0.30, 300) is None  # off when the lane does not set it


def late_dip_ask(w, t, close):
    from kalshi_moonshot import curverisk, watcher

    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_late_dip(t, risk, bid, close - left, left, 0.0, c)

    return ask


def test_integration_late_dip_sells_a_sharp_spot_dip_after_a_short_scaled_wait(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.late_dip_share, t.late_dip_units, t.late_dip_look_s, t.late_dip_wait_share = 0.25, 3.0, 30.0, 0.1
    ask = late_dip_ask(w, t, close)
    spot_feed(close - 100, 100.5)  # the spot safely over the target, 100 s left
    assert ask(0.95, 100) is None  # no dip yet
    spot_feed(close - 95, 99.95)  # the cushion drops by half a point in 5 s: many typical 10 s moves
    assert ask(0.90, 95) is None  # sharp, but a dip has to hold a tenth of the time left (9.5 s)
    spot_feed(close - 80, 99.95)
    why = ask(0.90, 80)  # held 15 s: over its 8 s wait
    assert why and why.startswith("late dip:") and "spot's cushion" in why


def test_integration_a_loss_sale_waits_while_the_spot_is_on_the_bets_side_of_the_target(tmp_path, monkeypatch, mem):
    # BNB and DOGE 13:30 YES, 2026-11-25: sold at 0.60 and 0.71 under a cost of 0.84 with the spot still over the strike; both settled YES (user: "just don't sell if it isn't on the other side of the target")
    w, t, close = take_target(tmp_path, monkeypatch, 0.84, unit=0.10)
    t.late_dip_share, t.late_dip_units, t.late_dip_look_s, t.late_dip_wait_share = 0.25, 3.0, 30.0, 0.1
    ask = late_dip_ask(w, t, close)
    spot_feed(close - 100, 100.5)  # the spot over the target (100.0)
    assert ask(0.84, 100) is None
    spot_feed(close - 95, 100.05)  # a sharp spot dip, but the spot is still over the target
    assert ask(0.71, 95) is None
    spot_feed(close - 80, 100.05)
    assert ask(0.71, 80) is None  # held past the wait: a loss sale (0.71 under 0.84) with the spot on the bet's side is vetoed
    spot_feed(close - 60, 99.8)  # the spot is now on the other side of the target
    why = ask(0.60, 60)
    assert why and why.startswith("late dip:")  # now it sells
    # a sale that is a gain is not this veto's business
    t.late_dip_since = 0.0
    t.entry = 0.60
    spot_feed(close - 50, 100.5)
    spot_feed(close - 45, 100.05)
    assert ask(0.80, 45) is None  # (the wait starts over; nothing here is vetoed, so it is the wait that holds it)


def test_integration_late_dip_a_bounce_starts_the_wait_over_and_early_is_not_its_part(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.late_dip_share, t.late_dip_units, t.late_dip_look_s, t.late_dip_wait_share = 0.25, 3.0, 30.0, 0.1
    ask = late_dip_ask(w, t, close)
    spot_feed(close - 400, 100.5)
    assert ask(0.95, 400) is None  # 400 s left: not the last quarter (225 s)
    spot_feed(close - 100, 100.5)
    assert ask(0.95, 100) is None
    spot_feed(close - 95, 99.95)
    assert ask(0.90, 95) is None  # sharp, waiting
    spot_feed(close - 90, 100.5)
    t.late_dip_spot = []  # the high of the look-back is gone with the bounce
    assert ask(0.95, 90) is None  # bounced: the wait is gone
    assert t.late_dip_since == 0.0


def test_integration_early_take_a_fall_must_hold_and_a_new_high_starts_it_over(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    thin_cushion(monkeypatch)  # a spot cushion that does not hold the bet: p_lose 0.3
    t.entry, t.early_take_share, t.early_take_stall_share, t.early_take_hold_share = 0.80, 1.0, 0.0, 0.05
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.90, 600) is None  # armed at its high
    assert ask(0.85, 590) is None  # fell 5c (over one wobble, 0.041): the fall starts, it has to hold 5% of 590 s = 29 s
    assert ask(0.93, 580) is None  # a new high: the fall is forgotten, the climb goes on
    assert ask(0.88, 570) is None  # a new fall starts
    assert ask(0.88, 550) is None  # 20 s of it: under 5% of 550 s (27 s)
    why = ask(0.88, 535)  # 35 s: held
    assert why and why.startswith("early take:")


def test_integration_early_take_a_spot_still_climbing_is_not_a_stall(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    t.entry, t.early_take_share, t.early_take_stall_share, t.early_take_hold_share = 0.80, 1.0, 0.1, 0.0
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left, level):
        spot_feed(close - left, level)
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.90, 800, 100.1) is None  # armed; the spot's cushion is 0.1
    assert ask(0.90, 760, 100.4) is None  # the bid is flat but the spot's cushion climbed more than a wobble of its own: the climb goes on
    assert ask(0.90, 700, 100.4) is None  # 100 s since the bid's high but 60 s since the spot's climb: under 10% of 700 s (70 s)
    assert ask(0.90, 650, 100.4) is None  # 110 s since the spot's last climb, over the 65 s wait, but the spot is still on the winning side: a stall never sells that (2026-11-25, user, on HYPE 20:30: "the same issue over and over")
    why = ask(0.90, 640, 99.9)  # the spot slipped to the wrong side of the target: now the stall sells
    assert why and "no new high" in why


def test_integration_early_take_a_fall_under_cost_ends_the_profit_run_and_a_recovery_is_not_sold_on_the_old_high(tmp_path, monkeypatch, mem):
    # DOGE 17:30 YES, 2026-11-25: bought 0.78, high 0.89, fell to 0.52, recovered to 0.82 and was sold at 0.81 on "fell 0.07 from its high 0.89", ten minutes old
    lose = [None]
    ask = _early_take_asker(tmp_path, monkeypatch, mem, 0.0, lose)  # entry 0.70
    assert ask(0.90, 700) is None  # armed: +20c
    assert ask(0.60, 600) is None  # under cost: the run is over, nothing sells at a loss
    assert ask(0.73, 500) is None  # back to +1c: under a wobble over cost, so not armed again, and the old 0.90 high is gone
    assert ask(0.74, 490) is None
    assert ask(0.82, 480) is None  # +10c over cost: armed again with a NEW high of 0.82
    why = ask(0.76, 470)  # now a real fall from the new high
    assert why and "bid fell" in why and "0.82" in why


def test_integration_early_take_holds_on_a_cushion_that_scales_with_the_time_left(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import watcher

    lose = [None]
    ask = _early_take_asker(tmp_path, monkeypatch, mem, 0.0, lose)
    spot = [(0.30, 0.10)]  # cushion 0.30 = three typical minutes of 0.10
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, t, now, avg_seconds=60: spot[0])
    ask.target.early_take_cushion_units = 1.0
    assert ask(0.90, 100) is None  # armed with 100 s left
    assert ask(0.80, 95) is None  # fell a wobble: three typical minutes over a bar of 1 x sqrt(95/60) = 1.3 typical minutes: it waits
    ask2 = _early_take_asker(tmp_path, monkeypatch, mem, 0.0, lose)
    ask2.target.early_take_cushion_units = 1.0
    assert ask2(0.90, 700) is None  # armed with 700 s left (SOL 17:00 NO, 2026-11-25: 1.4 to 2.5 typical minutes of cushion and 8 to 12 minutes left)
    assert ask2(0.80, 690) is None  # under the bar of 3.4 typical minutes, but the spot is still on the winning side: nothing in this step sells that (2026-11-26, user, on SOL 08:30 NO: "it was on the WINNING side, and it still sold it")
    spot[0] = (0.05, 0.10)
    ask3 = _early_take_asker(tmp_path, monkeypatch, mem, 0.0, lose)
    ask3.target.early_take_cushion_units = 1.0
    assert ask3(0.90, 100) is None
    assert ask3(0.79, 95) is None  # a thin cushion, still on the winning side: held
    spot[0] = (-0.05, 0.10)
    why = ask3(0.79, 94)  # the spot has crossed to the wrong side with the bet still in profit: the fall sells
    assert why and "bid fell" in why


def test_integration_no_recovery_waits_while_either_spot_reading_is_on_the_bets_side(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.82, unit=0.10)
    t.entry, t.no_recovery_share, t.bought_ts = 0.82, 0.2, close - 800.0
    avg60 = [(0.30, 0.10)]  # the spot's 60 s average over the target for the bet's side
    last5 = [(0.30, 0.10)]  # its last 5 s
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now, avg_seconds=60: avg60[0] if avg_seconds == 60 else last5[0])
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_no_recovery(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.80, 800) is None  # seen from the buy
    assert ask(0.53, 600) is None  # 200 s, past the wait, the bid is under cost, but the spot is 3 typical minutes on the bet's side: it waits
    avg60[0] = (0.04, 0.10)  # a thin cushion (BNB 13:30 YES, 2026-11-25: +0.4 typical minutes)
    assert ask(0.50, 590) is None
    avg60[0] = (-0.02, 0.10)  # the 60 s average has gone to the other side, but the last 5 s is still on the bet's side (DOGE 17:00 NO, 16:53:21: the spot had just come back under the strike)
    last5[0] = (0.01, 0.10)
    assert ask(0.45, 580) is None  # still waits: it cannot sell blindly (user, 2026-11-25)
    last5[0] = (-0.02, 0.10)  # both readings are on the other side
    why = ask(0.45, 570)
    assert why and "no recovery" in why
    assert "[facts read:" in why and "loss_while_ahead=no" in why and "spot=(-0.02" in why and "spot_now=(-0.02" in why  # the readings the veto was judged on are in the sale's note (2026-11-26)



def test_integration_no_recovery_counts_a_picked_up_bets_wait_from_when_it_was_first_watched(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.73, unit=0.10)
    t.entry, t.no_recovery_share, t.bought_ts = 0.73, 0.2, close - 800.0  # bought 800 s before the close, first watched 100 s later (a restart)
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_no_recovery(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.60, 700) is None  # first seen: the clock starts here
    assert ask(0.40, 600) is None  # 100 s since first seen: under the 180 s
    why = ask(0.30, 510)  # 190 s since first seen
    assert why and "no recovery" in why and "first watched" in why


def _wave_history(monkeypatch, path):
    """A tick memory holding `path` (a callable of seconds -> value) on a 1 s grid from 4000 s before the open to 1000 s after it."""
    from kalshi_moonshot import ticks

    OPEN = 900.0 * 2000
    T = [OPEN - 4000 + i for i in range(5000)]
    V = [path(t - OPEN) for t in T]

    class H:
        def window(self, index, a, b):
            import bisect

            lo, hi = bisect.bisect_left(T, a), bisect.bisect_right(T, b)
            return (T[lo:hi], V[lo:hi]) if lo < hi and T[0] <= a else None

    monkeypatch.setattr(ticks, "history", H())
    return OPEN


def test_unit_waves_before_reads_the_hour_before_the_open_in_the_markets_own_units(monkeypatch):
    import math

    from kalshi_moonshot import wavefacts

    wavefacts._before.clear()
    quick = _wave_history(monkeypatch, lambda s: 100.0 + 1.0 * math.sin(2 * math.pi * s / 360.0))  # a 6 minute wave of width about 0.7
    a = wavefacts.before("QUICK", quick)
    wavefacts._before.clear()
    slow = _wave_history(monkeypatch, lambda s: 100.0 + 5.0 * math.sin(2 * math.pi * s / 1200.0))  # a 20 minute wave five times as wide
    b = wavefacts.before("SLOW", slow)
    assert a and b and b["sd30"] > 3 * a["sd30"]  # the range differs ...
    assert b["w10"] > a["w10"]
    assert b["tau_s"] is None or a["tau_s"] is None or b["tau_s"] > a["tau_s"]  # ... and the slow wave decays more slowly
    wavefacts._before.clear()
    assert wavefacts.before("NONE", quick + 10 * 900.0) is None  # an hour the memory does not hold


def test_unit_waves_now_is_signed_for_the_side_and_reads_a_trend_as_a_trend(monkeypatch):
    from kalshi_moonshot import wavefacts

    opened = _wave_history(monkeypatch, lambda s: 100.0 + 0.01 * s)  # a straight climb
    up = wavefacts.now_read("IDX", opened + 600.0, 105.0, "yes", 300.0)
    dn = wavefacts.now_read("IDX", opened + 600.0, 105.0, "no", 300.0)
    assert up["r2"] > 0.95 and up["z"] > 0 > dn["z"]  # a trend: the line fits; the climb is toward YES and against NO
    assert up["slope"] > 0 > dn["slope"] and up["run5"] > 0 and dn["pull5"] > 0
    assert wavefacts.now_read("IDX", opened - 4500.0, 105.0, "yes", 300.0) is None  # not in memory


def test_unit_the_board_serves_the_wave_facts_to_the_steps_that_name_them(monkeypatch):
    from kalshi_moonshot import facts, wavefacts

    opened = _wave_history(monkeypatch, lambda s: 100.0 + 0.01 * s)

    class T:
        side, shock_index, shock_ref, close_ts, ticker = "yes", "IDX", 105.0, opened + 900.0, "KXBTC15M-30JAN010015-15"

    wavefacts._before.clear()
    b = facts.Board(None, T(), opened + 600.0, 300.0, 0.5)
    view = b.view("value_sale")
    assert view.get("waves_before")["sd30"] > 0 and view.get("wave_now")["z"] != 0
    import pytest

    with pytest.raises(KeyError):
        b.view("late_dip").get("wave_now")  # a step that did not name it cannot read it
    facts.check_needs(["priskp", "value_sale", "wave_take", "value_hold"], "test")


def test_unit_wave_leg_names_the_leg_the_side_is_in_and_refuses_only_a_leg_at_its_extreme(monkeypatch):
    import math

    from kalshi_moonshot import wavefacts

    wavefacts._before.clear()
    wavefacts._big.clear()
    # a wavy hour before the open, then a fall of 0.02 a second for 150 s from the open: a falling swing that has not turned
    opened = _wave_history(monkeypatch, lambda s: 100.0 + 0.3 * math.sin(2 * math.pi * s / 120.0) if s < 0 else 100.0 - 0.02 * min(s, 150.0))
    now = opened + 150.0
    no = wavefacts.leg("IDX", opened, now, 99.0, "no", 750.0)
    yes = wavefacts.leg("IDX", opened, now, 99.0, "yes", 750.0)
    assert no["class"] == "inside" and no["swing_u"] >= 0.5 and no["would_refuse"] is True  # a NO inside the dip, in a wavy market: the 08:30 HYPE, BNB and ETH shape
    assert yes["class"] == "against" and yes["would_refuse"] is True  # a YES against the fall
    wavefacts._before.clear()
    wavefacts._big.clear()
    # a straight climb the whole hour and after: the YES is inside a leg too, but no usual big leg or wave travel has been outrun: a normal favourite buy, not refused
    opened = _wave_history(monkeypatch, lambda s: 100.0 + 0.02 * s)
    got = wavefacts.leg("IDX", opened, opened + 300.0, 105.0, "yes", 600.0)
    assert got is not None and got["r2"] is not None and got["r2"] >= wavefacts.R2_CUT and got["would_refuse"] is False
    assert wavefacts.leg("IDX", opened + 10 * 900.0, opened + 10 * 900.0 + 300.0, 105.0, "yes", 600.0) is None  # not in memory


def test_unit_wave_leg_refuses_an_unturned_dip_and_passes_what_it_cannot_read(monkeypatch):
    import math

    from kalshi_moonshot import binaryrisk, wavefacts

    wavefacts._before.clear()
    opened = _wave_history(monkeypatch, lambda s: 100.0 + 0.3 * math.sin(2 * math.pi * s / 120.0) if s < 0 else 100.0 - 0.02 * min(s, 150.0))
    monkeypatch.setattr(binaryrisk, "curve_index", lambda series: "IDX")
    monkeypatch.setattr(ticks_mod.strikes, "get", lambda tk, *a: 99.0)
    from kalshi_moonshot import buychain, config, lanes
    from kalshi_moonshot.strategy import LongShot

    sp = lanes.LaneSpec("x", "ladder", "x", (0.60, 0.92), (0, 1), 0.05, 0.0, sides=("yes", "no"))
    left = 750.0
    sh = LongShot("KXGOLD15M-30JAN010015-15", "t", "", 0.79, 0.80, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="x", series="KXGOLD15M", side="no", event_ticker="KXGOLD15M-30JAN010015")
    c = buychain.STEPS["wave_leg"](buychain.Ctx(config.load(), sp, now=opened + 150.0), sh)
    assert c.ok is False and "inside" in c.why and "not bought" in c.why  # a NO inside the dip in a wavy market is refused (2026-11-25, the 08:30 and 11:30 rounds)
    monkeypatch.setattr(ticks_mod.strikes, "get", lambda tk, *a: None)
    assert buychain.STEPS["wave_leg"](buychain.Ctx(config.load(), sp, now=opened + 150.0), sh).ok is True  # no target in memory: no read is no refusal

def test_unit_wave_leg_refuses_an_against_or_turned_bet_always_and_an_inside_bet_only_at_its_extreme(monkeypatch):
    from kalshi_moonshot import wavefacts

    wavefacts._before.clear()
    wavefacts._big.clear()
    # a steady climb for the hour, then a dip from 560 s that runs against a YES: 40 s in it is part-way through its leg, but it is AGAINST the bet: it waits (ZEC 18:15 NO, 2026-11-25: bought as the dip turned up)
    path = lambda s: 100.0 + 0.01 * s - (0.04 * (s - 560.0) if s > 560.0 else 0.0)
    opened = _wave_history(monkeypatch, path)
    young = wavefacts.leg("IDX", opened, opened + 600.0, 100.0, "yes", 300.0)
    assert young is not None and young["class"] == "against" and young["travel"] < wavefacts.EXTREME
    assert young["would_refuse"] is True  # against: always
    # the same market for a NO: the dip runs WITH it and is young, so it is inside and part-way: bought
    no = wavefacts.leg("IDX", opened, opened + 600.0, 100.0, "no", 300.0)
    assert no["class"] == "inside" and no["would_refuse"] is False
    old = wavefacts.leg("IDX", opened, opened + 800.0, 100.0, "no", 100.0)
    assert old["class"] == "inside" and old["travel"] >= wavefacts.EXTREME and old["would_refuse"] is True  # inside, but at its extreme



def test_unit_curve_verdict_waits_at_a_dip_and_buys_a_trend_only_in_a_curve(monkeypatch):
    from kalshi_moonshot import wavefacts

    wavefacts._before.clear()
    wavefacts._big.clear()
    # a steady climb for the hour, then a dip from 560 s: the target (100) is within a usual wave of the spot: a CURVE
    path = lambda s: 100.0 + 0.01 * s - (0.04 * (s - 560.0) if s > 560.0 else 0.0)
    opened = _wave_history(monkeypatch, path)
    yes = wavefacts.curve("IDX", opened, opened + 600.0, 100.0, "yes", 300.0)
    assert yes is not None and yes["class"] == "against"
    if yes["regime"] == "curve":
        assert yes["verdict"] == "wait"  # the dip runs against the YES: wait, not buy
    # a flat-with-cushion market: the target is far below the spot: STEADY, the lane's own checks decide (buy whenever the price is good)
    far = wavefacts.curve("IDX", opened, opened + 600.0, 50.0, "yes", 300.0)
    assert far is not None and far["regime"] == "steady" and far["verdict"] == "steady"
    assert wavefacts.curve("IDX", opened + 10 * 900.0, opened + 10 * 900.0 + 300.0, 100.0, "yes", 300.0) is None  # not in memory: the lane's checks decide


def test_unit_curve_trend_needs_a_turn_before_it(monkeypatch):
    import math

    from kalshi_moonshot import wavefacts

    wavefacts._before.clear()
    wavefacts._big.clear()
    # a wavy hour, then a plain fall from the open: a NO inside a fall that never turned from anything (no swing before it): not a trend entry
    opened = _wave_history(monkeypatch, lambda s: 100.0 + 0.3 * math.sin(2 * math.pi * s / 120.0) if s < 0 else 100.0 - 0.02 * min(s, 150.0))
    got = wavefacts.curve("IDX", opened, opened + 150.0, 99.0, "no", 750.0)
    assert got is not None and got["class"] == "inside" and got["prev_u"] < wavefacts.TREND_MIN
    assert got["verdict"] != "buy"


def test_unit_curve_verdict_is_mirror_symmetric(monkeypatch):
    """User, 2026-11-25 ~19:40: "every loss I look at is down, in a dip; something is wrong with the calculations that it is only buying down". A path and its mirror image about the target must give the YES verdict on one exactly what the NO verdict is on the other, at every second."""
    import math

    from kalshi_moonshot import wavefacts

    wavy = lambda s: 100.0 + 0.4 * math.sin(2 * math.pi * s / 150.0) + (0.003 * s if s > 0 else 0.0) + (0.6 * math.sin(2 * math.pi * s / 400.0))
    bad, read = [], 0
    for f, g in ((wavy, lambda s: 200.0 - wavy(s)),):
        for t in range(120, 800, 40):
            for side, other in (("yes", "no"), ("no", "yes")):
                wavefacts._before.clear()
                wavefacts._big.clear()
                opened = _wave_history(monkeypatch, f)
                a = wavefacts.curve("IDX", opened, opened + t, 100.2, side, 900.0 - t)
                wavefacts._before.clear()
                wavefacts._big.clear()
                opened = _wave_history(monkeypatch, g)
                b = wavefacts.curve("IDX", opened, opened + t, 200.0 - 100.2, other, 900.0 - t)
                key = lambda c: None if c is None else (c["class"], c["regime"], c["verdict"], c["swing_u"], c["prev_u"], c["at_extreme"])
                read += a is not None
                if key(a) != key(b):
                    bad.append((t, side, key(a), key(b)))
    assert read > 10 and not bad, (read, bad[:5])


def test_integration_early_take_one_wobble_fall_with_time_left_is_not_a_stop_and_a_bigger_one_is(tmp_path, monkeypatch, mem):
    """2026-11-25 19:38 ET, user, on DOGE 19:45 NO bought at 0.76: the bid ran to 0.86, fell 0.04 (one wobble of 0.038) with 396 s left and it was sold at 0.82, +3.7c. A fall now has to be more than one wobble early in the market (3 at the open, 2 with half the time left, 1 at the close)."""
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.76, unit=0.093)  # the bid's wobble: 0.093 x sqrt(10/60) = 0.038
    thin_cushion(monkeypatch)
    t.entry, t.early_take_share, t.early_take_stall_share, t.early_take_hold_share = 0.76, 1.0, 0.0, 0.0
    t.risk_unit, t.risk_unit_at = 0.093, 1e12  # the cached unit: the bid's wobble is read from it, not from quotes
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.86, 0.80, None)
    left = 396.0

    def ask(bid):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.86) is None  # armed and climbing: +10c
    assert ask(0.82) is None  # fell 0.04 from 0.86, one wobble, with 396 s left: a fall needs about 0.071: held (the DOGE sale)
    assert ask(0.90) is None  # a new high
    assert ask(0.86) is None  # fell 0.04 again: held
    why = ask(0.82)  # fell 0.08 from 0.90: more than the 0.071 a fall needs now, still in profit
    assert why and why.startswith("early take:") and "stopped climbing" in why


def test_integration_a_loss_while_ahead_is_held_by_asking_the_veto_not_by_the_walks_refusal(tmp_path, monkeypatch, mem, caplog):
    """2026-11-26: value_sale built a sale on NEAR every second and the walk's belt-and-braces refusal threw it out with an error line each time. The step asks the board itself now: a sale under what the bet cost with the spot still on the bet's side is held quietly (user, 2026-11-25: "just don't sell if it isn't on the other side of the target")."""
    w, t, close = take_target(tmp_path, monkeypatch, 0.95, unit=0.10)
    spot_feed(close - 300, 100.3)  # the spot is over the target on the bet's side: holding is worth about 0.81
    assert asker(w, t, close)(0.85, 300) is None  # the bid pays about 0.84, over the hold value, but under the 0.95 the bet cost
    assert "sold against the veto" not in caplog.text
    spot_feed(close - 300, 99.7)  # the spot has crossed to the wrong side: the same loss is sold
    assert asker(w, t, close)(0.50, 300) is not None


def test_integration_early_take_never_sells_with_the_spot_ahead_and_sells_once_it_crosses(tmp_path, monkeypatch, mem):
    """2026-11-25 20:30 ET, user, on HYPE 20:30 YES (sold at 0.92 with 45 s left on a 4 s stall): a pause in the bid is not a reason to sell while the spot is on the winning side; a FALL from the high still is."""
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.80, unit=0.10)
    thin_cushion(monkeypatch)
    t.entry, t.early_take_share, t.early_take_stall_share, t.early_take_hold_share = 0.80, 1.0, 0.1, 0.0
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left, level):
        spot_feed(close - left, level)
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_early_take(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.92, 300, 100.3) is None  # armed, spot ahead
    for left in (240, 180, 120, 60, 45):
        assert ask(0.92, left, 100.3) is None  # the bid is flat for minutes, the spot sits ahead: held
    assert ask(0.86, 44, 100.3) is None  # a real fall of 0.06 from the 0.92 high with the spot still ahead: held too (2026-11-26, SOL 08:30 NO)
    why = ask(0.86, 43, 99.9)  # the spot crossed to the wrong side, the bet still in profit: now it sells
    assert why and why.startswith("early take:")


def test_unit_travel_left_grows_with_the_time_left():
    """2026-11-26, user: "if you're betting on a curve early you need to be calculating for additional swings and variance". How far the market's waves usually travel in the time left: the 3, 10 and 15 minute travels joined, grown by sqrt beyond 15 minutes, shrunk by sqrt under 3."""
    from kalshi_moonshot import wavefacts

    bef = {"w3": 0.10, "w10": 0.20, "w15": 0.25}
    assert wavefacts.travel_left(None, 600.0) is None and wavefacts.travel_left({"w3": 0.1}, 600.0) is None
    assert abs(wavefacts.travel_left(bef, 180.0) - 0.10) < 1e-9 and abs(wavefacts.travel_left(bef, 600.0) - 0.20) < 1e-9 and abs(wavefacts.travel_left(bef, 900.0) - 0.25) < 1e-9
    assert wavefacts.travel_left(bef, 780.0) > wavefacts.travel_left(bef, 600.0) > wavefacts.travel_left(bef, 400.0) > wavefacts.travel_left(bef, 120.0)
