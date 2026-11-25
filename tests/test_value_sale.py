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
    assert facts.NEEDS["value_sale"] == ("p_lose", "spot")


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
    why = ask(0.90, 650, 100.4)  # 110 s since the spot's last climb: over the 65 s wait
    assert why and "spot's cushion" in why


def test_integration_early_take_waits_while_the_spot_cushion_is_a_typical_minute_or_more(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import watcher

    lose = [None]
    ask = _early_take_asker(tmp_path, monkeypatch, mem, 0.0, lose)
    spot = [(0.30, 0.10)]  # cushion 0.30 = three typical minutes of 0.10
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, t, now: spot[0])
    ask.target.early_take_cushion_units = 1.0
    assert ask(0.90, 700) is None  # armed
    assert ask(0.80, 690) is None  # fell a wobble, but the cushion is 3 typical minutes: it waits
    spot[0] = (0.05, 0.10)  # the cushion has thinned to half a minute
    why = ask(0.79, 680)
    assert why and "bid fell" in why


def test_integration_no_recovery_waits_while_the_spot_cushion_is_a_typical_minute_or_more(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher

    w, t, close = take_target(tmp_path, monkeypatch, 0.82, unit=0.10)
    t.entry, t.no_recovery_share, t.bought_ts, t.no_recovery_cushion_units = 0.82, 0.2, close - 800.0, 1.0
    spot = [(0.30, 0.10)]
    monkeypatch.setattr(watcher.ProfitWatcher, "_wave_spot", lambda self, tt, now: spot[0])
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None
        with memguard.decision("sell"):
            return w._sell_no_recovery(t, risk, bid, close - left, left, 0.0, c)

    assert ask(0.80, 800) is None  # seen from the buy
    assert ask(0.53, 600) is None  # 200 s, past the wait, but a 3 minute cushion: it waits
    spot[0] = (0.04, 0.10)  # the cushion is gone
    why = ask(0.50, 590)
    assert why and "no recovery" in why
