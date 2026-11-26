"""The turn sale (sell step `turn_sale`, waveturn.peek; 2026-11-26): ZEC 10:30 NO was bought at 0.18 at a peak, its bid ran to 0.30-0.32 two minutes later as the wave came back, and nothing sold it (the give-up took 0.02). A wave bet in profit is sold when the wave has turned back, read on the buy step's own curve."""

from kalshi_moonshot import curverisk, memguard, sellchain, watcher, waveturn
from kalshi_moonshot import ticks as ticks_mod
from tests.test_wave_turn import INDEX, REPEATED, K, feed, fires, hour_path, mem, u_of, window_path  # noqa: F401  (mem: the fixture)

# REPEATED: a rise of 2 u to second 60, a fall of 2 u to second 120, a rise to 180, a fall to 240, then flat


def path(opened):
    hour = hour_path()
    feed(hour, window_path(REPEATED, u_of(hour)), opened=opened)


def test_the_step_is_known_and_may_rest_at_the_ask():
    from kalshi_moonshot import restsell

    sellchain.check(("value_hold", "turn_sale", "wave_take", "wave_salvage", "value_sale"), "x")
    assert restsell.eligible("turn sale: the wave turned back") and "turn_sale" in watcher._SELL


def test_unit_a_no_bought_at_the_peak_has_its_turn_when_the_fall_gives_back(mem):
    from tests.test_wave_turn import OPEN

    path(OPEN)

    def at(t, side="no", bought=70):
        r = waveturn.peek(INDEX, OPEN, OPEN + t + 0.5, K)
        return r is not None and waveturn.turned_against(r, side, bought)

    assert not at(100)  # the fall is still running: nothing given back
    assert at(145)  # past the low at 120 and 0.2 u off it: the wave has turned back
    assert not at(145, side="yes")  # a YES wants a rise that turned down, not this
    assert not at(145, bought=130)  # a bet bought after the low: that turn is not its own
    assert at(205, side="yes", bought=130)  # the YES bought near the low has its turn after the next peak at 180


def test_unit_asking_for_the_reading_does_not_use_up_a_swings_buy(mem):
    from tests.test_wave_turn import OPEN

    path(OPEN)
    for t in range(60, 260):
        waveturn.peek(INDEX, OPEN, OPEN + t + 0.5, K)
    assert fires() == [(141, "yes"), (201, "no")]  # the buy side fires as it does with no sell side asking


def held_no(tmp_path, monkeypatch, entry=0.18, live=True):
    from tests.test_risk_exit_flow import CLOSE, TICKER, held

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['turn_sale']" + ("\nturn_sale_live=true" if live else ""))
    t = w.targets[(TICKER, "risk_only")]
    opened = CLOSE - 900.0
    assert t.turn_sale_live is live  # the lane file reached the target
    t.side, t.entry, t.bought_ts, t.strike = "no", entry, opened + 70.0, K
    path(opened)
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")

    def ask(sec, bid):
        now = opened + sec + 0.5
        with memguard.decision("sell"):
            return w._risk_decide(t, risk, bid, now, CLOSE - now, 0.0)

    return ask, t


def test_integration_the_bet_is_sold_at_a_gain_when_the_wave_turns_back_and_not_before(tmp_path, monkeypatch, mem):
    ask, _t = held_no(tmp_path, monkeypatch)
    assert ask(100, 0.30) is None  # ahead, but the fall is still running: held
    why = ask(145, 0.30)
    assert why and why.startswith("turn sale: the wave turned back: the spot fell") and "0.18 paid" in why and "sold at turn_sale" in why
    assert "after both fees" in why and "s left" in why


def test_integration_it_is_never_a_loss_sale(tmp_path, monkeypatch, mem):
    ask, _t = held_no(tmp_path, monkeypatch)
    assert ask(145, 0.15) is None  # the wave turned back but the bid is under what the bet cost: this step does not sell it
    assert ask(145, 0.19) is None  # one cent over the price paid is still a loss after both fees


def test_integration_a_yes_bet_mirrors_it(tmp_path, monkeypatch, mem):
    ask, t = held_no(tmp_path, monkeypatch)
    from tests.test_risk_exit_flow import CLOSE

    t.side, t.bought_ts = "yes", CLOSE - 900.0 + 130.0  # bought after the low at 120
    assert ask(170, 0.40) is None  # the rise is still running
    why = ask(205, 0.40)
    assert why and why.startswith("turn sale: the wave turned back: the spot rose")


def test_integration_no_reading_is_no_sale(tmp_path, monkeypatch, mem):
    ask, _t = held_no(tmp_path, monkeypatch)
    ticks_mod.history.clear()
    waveturn.clear()
    assert ask(145, 0.30) is None


def test_integration_without_its_own_switch_the_step_only_logs_what_it_would_do(tmp_path, monkeypatch, mem, caplog):
    import logging

    ask, _t = held_no(tmp_path, monkeypatch, live=False)
    with caplog.at_level(logging.INFO, logger="moonshot"):
        assert ask(145, 0.30) is None and ask(146, 0.30) is None  # the lane's exit is live, this step is not: no sale
    assert caplog.text.count("turn sale shadow:") == 1 and "would sell: turn sale: the wave turned back" in caplog.text  # said once a bet
