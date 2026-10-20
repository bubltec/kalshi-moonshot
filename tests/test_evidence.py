import random

import pytest

from kalshi_moonshot import evidence as ev
from kalshi_moonshot.store import Store

T0 = 1_792_000_000 // 900 * 900


def window(i, mids, result, series="KXGOLD15M", spread=0.01):
    """A window whose YES mid at minutes 1..15 is `mids`."""
    path = {k: (m - spread / 2, m + spread / 2, m - spread / 2, m + spread / 2) for k, m in enumerate(mids, 1)}
    return ev.Window(series, T0 + i * 900, result, path)


def ramp(start, end, n=15):
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def setting(entry, exit_=None, side=("yes",), price=(0.05, 0.95), minutes=(3, 10)):
    return ev.Setting("t", side, price, minutes, 0.05, tuple(sorted(entry.items())), tuple(sorted((exit_ or {"kind": "hold"}).items())))


def test_a_slope_entry_buys_when_the_price_is_climbing_and_not_before():
    flat_then_up = [0.40] * 5 + ramp(0.40, 0.80, 10)
    w = window(0, flat_then_up, "yes")
    trades = ev.run_setting([w], setting({"kind": "slope", "direction": "follow", "window": 2, "min": 0.03}))
    assert len(trades) == 1
    t = trades[0]
    assert t.got == 1.0 and 0.4 < t.cost < 0.8  # bought partway up the ramp, held to a YES settlement


def test_following_and_fading_pick_opposite_sides_of_the_same_move():
    w = window(0, ramp(0.30, 0.70), "yes")
    follow = ev.run_setting([w], setting({"kind": "slope", "direction": "follow", "window": 3, "min": 0.02}))
    fade = ev.run_setting([w], setting({"kind": "slope", "direction": "fade", "window": 3, "min": 0.02}))
    assert follow and not fade  # YES is rising; fading would need it to fall
    both = ev.run_setting([w], setting({"kind": "slope", "direction": "follow", "window": 3, "min": 0.02}, side=("yes", "no")))
    assert len(both) == 1  # the NO side is falling, so only YES triggers


def test_no_side_is_scored_from_its_own_prices():
    w = window(0, [0.70] * 15, "no")  # YES at 70c, so NO costs about 30c and wins
    (t,) = ev.run_setting([w], setting({"kind": "always"}, side=("no",), price=(0.20, 0.40)))
    assert t.got == 1.0 and 0.29 < t.cost < 0.33


def test_the_price_band_and_spread_filters_apply():
    w = window(0, [0.95] * 15, "yes")
    assert ev.run_setting([w], setting({"kind": "always"}, price=(0.10, 0.50))) == []
    wide = window(0, [0.40] * 15, "yes", spread=0.20)
    assert ev.run_setting([wide], setting({"kind": "always"})) == []  # spread over the 5c limit


def test_exits_hold_trail_target_stop_and_slope_turn():
    up_then_down = ramp(0.30, 0.60, 8) + ramp(0.60, 0.20, 7)
    w = window(0, up_then_down, "no")
    base = {"kind": "always"}
    hold = ev.run_setting([w], setting(base, {"kind": "hold"}, minutes=(2, 2)))[0]
    assert hold.got == 0.0  # held a YES that lost
    trail = ev.run_setting([w], setting(base, {"kind": "trail", "drop": 0.05}, minutes=(2, 2)))[0]
    assert 0.40 < trail.got < 0.62  # sold on the way down, near the peak
    turn = ev.run_setting([w], setting(base, {"kind": "slope_turn", "ticks": 1}, minutes=(2, 2)))[0]
    assert 0.40 < turn.got < 0.62
    target = ev.run_setting([w], setting(base, {"kind": "target", "multiple": 1.5}, minutes=(2, 2)))[0]
    assert target.got == pytest.approx(0.345 * 1.5 - 0.07 * (0.345 * 1.5) * (1 - 0.345 * 1.5), abs=0.06)
    stop = ev.run_setting([w], setting(base, {"kind": "stop", "drop": 0.05}, minutes=(2, 2), price=(0.05, 0.95)))[0]
    assert stop.got > 0.0  # stopped out before the collapse only if it fell 5c first; it rose, so it rode the fall
    # a stop that never triggers on a steadily rising price holds to settlement
    rising = window(1, ramp(0.30, 0.90), "yes")
    assert ev.run_setting([rising], setting(base, {"kind": "stop", "drop": 0.05}, minutes=(2, 2)))[0].got == 1.0


def test_a_resting_sell_fills_on_the_minutes_best_bid_not_its_close():
    path = dict.fromkeys(range(1, 16), (0.19, 0.21, 0.19, 0.21))
    path[6] = (0.20, 0.22, 0.45, 0.22)  # the bid touched 45c inside minute 6 and closed back at 20c
    w = ev.Window("X", T0, "no", path)
    (t,) = ev.run_setting([w], setting({"kind": "always"}, {"kind": "target", "multiple": 2.0}, minutes=(2, 2)))
    assert t.got == pytest.approx(0.42 - 0.07 * 0.42 * 0.58)


def test_fees_are_charged_on_entry_and_on_a_sale():
    w = window(0, [0.50] * 15, "yes")
    (hold,) = ev.run_setting([w], setting({"kind": "always"}, {"kind": "hold"}))
    assert hold.cost == pytest.approx(0.505 + 0.07 * 0.505 * 0.495)
    (sell,) = ev.run_setting([w], setting({"kind": "always"}, {"kind": "time", "minute": 12}))
    assert sell.got == pytest.approx(0.495 - 0.07 * 0.495 * 0.505)


# ---- statistics, rule files and the verdict ----
def test_stats_report_roi_win_rate_and_an_interval_that_widens_with_noise():
    trades = [ev.Trade(0.5, 1.0 if i % 2 else 0.0, T0 + i * 900, "X") for i in range(200)]
    s = ev.stats(trades)
    assert s.n == 200 and s.roi == pytest.approx(0.0, abs=0.05) and s.win == pytest.approx(0.5)
    assert s.lo < s.roi < s.hi
    assert ev.stats([]).n == 0


def test_a_grid_expands_every_combination():
    rules = ev.parse_rules("""
[rule.a]
entry = { kind = "slope", window = [2, 3], min = [0.01, 0.02, 0.04], direction = "follow" }
exit = { kind = "trail", drop = [0.03, 0.05] }
""")
    assert len(rules[0].settings) == 12
    assert len({s.entry for s in rules[0].settings}) == 6


@pytest.mark.parametrize(("toml", "msg"), [
    ('[rule.a]\nentry = { kind = "vibes" }', "entry kind"),
    ('[rule.a]\nexit = { kind = "yolo" }', "exit kind"),
    ('[rule.a]\nside = "up"', "side must be"),
    ('[rule.a]\nbogus = 1', "unknown setting"),
])
def test_bad_rule_files_fail_loudly(toml, msg):
    with pytest.raises(ValueError, match=msg):
        ev.parse_rules(toml)


def world(n, edge, seed=1):
    """Windows where a rising price is followed by a YES win with probability 0.5 + edge."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        up = rng.random() < 0.5
        mids = ramp(0.45, 0.55 if up else 0.35)
        yes = rng.random() < (0.5 + edge if up else 0.5 - edge)
        out.append(window(i, mids, "yes" if yes else "no"))
    return out


def test_a_real_signal_is_supported_and_no_signal_is_not():
    rule = ev.parse_rules("""
[rule.follow]
side = "yes"
price = [0.3, 0.7]
entry = { kind = "slope", direction = "follow", window = 3, min = 0.005 }
""")[0]
    strong = world(1500, edge=0.25)
    tr, te = ev.split_windows(strong, 0.6)
    v = ev.evaluate(rule, tr, te)
    assert v.label == "SUPPORTED" and v.chosen.test.lo > 0
    flat = world(1500, edge=0.0)
    tr, te = ev.split_windows(flat, 0.6)
    assert ev.evaluate(rule, tr, te).label in {"NOT SUPPORTED", "NOT PROFITABLE", "PROMISING"}
    assert ev.evaluate(rule, tr, te).label != "SUPPORTED"


def test_too_few_trades_is_reported_not_guessed():
    rule = ev.parse_rules('[rule.a]\nentry = { kind = "slope", direction = "follow", window = 3, min = 0.5 }')[0]
    tr, te = ev.split_windows(world(300, 0.2), 0.6)
    assert ev.evaluate(rule, tr, te).label == "NOT ENOUGH DATA"


def test_windows_load_from_the_candle_tables(tmp_path):
    store = Store(tmp_path / "m.db")
    store.db.execute("INSERT INTO windows VALUES ('KXGOLD15M-A','KXGOLD15M',?,100.0,'yes',0)", (T0 + 900,))
    for k in range(1, 16):
        store.db.execute(
            "INSERT INTO candles (ticker,end_ts,bid_c,ask_c,bid_lo,ask_hi,last_c,volume,oi,bid_hi,ask_lo) "
            "VALUES ('KXGOLD15M-A',?,0.49,0.51,0.48,0.52,0.5,10,5,0.50,0.50)", (T0 + k * 60,))
    store.db.commit()
    (w,) = ev.load_windows(store)
    assert w.series == "KXGOLD15M" and w.result == "yes" and len(w.path) == 15
    assert ev.load_windows(store, ["KXBTC15M"]) == []


def test_the_packaged_rules_parse():
    from importlib import resources

    rules = ev.parse_rules(resources.files("kalshi_moonshot").joinpath("rules.toml").read_text())
    assert {r.name for r in rules} >= {"follow_the_slope", "fade_the_slope", "cheap_side_take_profit"}
    assert sum(len(r.settings) for r in rules) >= 30
