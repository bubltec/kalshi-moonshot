"""The one exit rule (fallrule.py; user, 2026-11-26: "we need to consolidate one rule that handles them all"): early profit that fades with the time elapsed, no loss while the spot has not crossed, salvage on a failed bounce, the time backstop and the top. Unit tests on the pure function, then the watcher's step through a held bet."""
from kalshi_moonshot import fallrule, memguard
from kalshi_moonshot.strategy import fee_per_contract as fee


def asker(entry, wobble=0.043, spot_unit=10.0):
    s = fallrule.State()

    def ask(bid, left, cushion=None, cross=None):
        return fallrule.decide(s, bid, entry, fee(bid), fee(entry), fee, wobble, 900.0 - left, left, cushion, spot_unit if cushion is not None else None, cross)

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
    fades = [fallrule.fade(900.0 - 60.0 * m) for m in range(0, 15)]
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
    assert ask(0.99, 30).startswith("one rule, the top:")
