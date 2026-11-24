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
    V = [100.0 if x <= close - 900.0 + 5 else 99.7 + 0.01 * (-1) ** int(x) for x in T]  # the perp: 100.0 at the open, 99.7 now
    ticks_mod.history.warm("KP_BTC", list(zip(T, V, strict=True)), T[0] - 1.0)
    assert asker(w, t, close)(0.50, 300) is None  # off: the settlement index says the spot is on the bet's side
    t.spot_perp = True
    assert asker(w, t, close)(0.50, 300) is not None  # on: the perp is under its open, the wrong side
    ticks_mod.history.clear()
    spot_feed(now, 100.3)
    t.perp_ref = 0.0
    assert asker(w, t, close)(0.50, 300) is None  # no perp reading: the settlement index again


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
