"""The facts board (facts.py; 2026-11-22): named facts steps read instead of calling each other, vetoes that stop a step selling, and the weights ledger."""
import pytest

from kalshi_moonshot import buychain, facts, memguard, sellchain, watcher
from tests.test_wave_top import asker, mem, spot_feed, switch_target, take_target  # noqa: F401  (mem: the fixture)


class Host:
    """The watcher's reading helpers the readers call, counted."""

    def __init__(self):
        self.calls = 0

    def _risk_unit(self, t, now):
        self.calls += 1
        return 0.1


class T:
    wave_cushion = True
    ticker = "X"


def test_every_fact_a_step_names_has_a_reader_and_a_missing_one_is_refused_at_load():
    for step, needs in facts.NEEDS.items():
        assert (step in sellchain.SELL_STEPS or step in buychain.STEPS) and all(n in facts.READERS for n in needs)
    sellchain.check(("wave_take", "wave_salvage"), "lane x")  # loads
    facts.NEEDS["wave_take"] = (*facts.NEEDS["wave_take"], "no_such_fact")
    try:
        with pytest.raises(ValueError, match="no_such_fact"):
            sellchain.check(("wave_take",), "lane x")
    finally:
        facts.NEEDS["wave_take"] = facts.NEEDS["wave_take"][:-1]


def test_a_fact_is_read_once_per_check_and_a_step_can_only_read_the_facts_it_named():
    host = Host()
    b = facts.Board(host, T(), 100.0, 600.0, 0.3)
    v = b.view("wave_take")
    assert v.get("bid_unit") == 0.1 and v.get("bid_unit") == 0.1 and host.calls == 1  # computed once, read twice
    with pytest.raises(KeyError, match="did not name"):
        v.get("spot")  # wave_take does not name the raw spot
    with pytest.raises(KeyError, match="no reader"):
        b.get("nothing")
    assert "bid_unit=0.1" in b.note()


def test_a_reader_that_fails_is_an_unknown_fact_not_a_crash():
    class Bad:
        def _wave_spot(self, t, now):
            raise RuntimeError("boom")

    b = facts.Board(Bad(), T(), 100.0, 600.0, 0.3)
    assert b.get("spot") is None and "unreadable" in b.why("spot")
    assert b.get("p_lose") is None and b.get("cushion_factor") == 1.0  # facts built on it fall back


def test_weights_accumulate_on_the_board_and_show_in_the_note():
    b = facts.Board(Host(), T(), 100.0, 600.0, 0.3)
    assert b.score() == 0.0 and b.note() == ""
    b.view("wave_take").weigh(0.6, "the staircase says sell")
    b.weigh("wave_salvage", -0.2, "the spot is still moving its way")
    assert b.score() == pytest.approx(0.4)
    assert "wave_take +0.60" in b.note() and "wave_salvage -0.20" in b.note() and "= +0.40" in b.note()


def test_a_veto_stops_a_step_and_the_walk_refuses_a_step_that_sells_anyway(tmp_path, monkeypatch, mem, caplog):
    from kalshi_moonshot import curverisk

    w, t, close = switch_target(tmp_path, monkeypatch)
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    t.sell_chain = ("wave_salvage",)
    monkeypatch.setattr(facts, "VETO_RULES", [("whipsaw", lambda v: v is True, ("wave_salvage",), "a test veto")])  # the wave lane has no veto now; the machinery is tested with one
    monkeypatch.setattr(facts, "VETOED_STEPS", frozenset({"wave_salvage"}))
    t.wave_kind = "whipsaw"
    monkeypatch.setitem(watcher._SELL, "wave_salvage", lambda self, tt, r, bid, now, left, pnl, c: "wave salvage: a sale that ignores the board")
    now = close - 700.0
    with memguard.decision("sell"):
        assert w._risk_decide(t, risk, 0.30, now, close - now, 0.0) is None
    assert "sold against the veto" in caplog.text
    t.wave_whipsaw = 0.0  # the switch off: nothing vetoes, the same step's sale goes through
    t.wave_kind = ""
    with memguard.decision("sell"):
        got = w._risk_decide(t, risk, 0.30, now + 1.0, close - now - 1.0, 0.0)
    assert got and got.startswith("wave salvage")


def test_the_sale_reason_carries_the_facts_it_was_judged_on(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    ask = asker(w, t, close)
    spot_feed(close - 2.0, 98.0)
    for bid in (0.36, 0.42, 0.35, 0.41, 0.48, 0.41, 0.36):
        ask(bid, 60)
    why = ask(0.34, 60)
    assert why and "[facts read:" in why and "cushion_factor=" in why and "whipsaw=no" in why


def test_inside_the_settlement_window_p_lose_reads_where_the_window_would_end():
    """2026-11-26 (user, on ZEC 15:45 YES: "I'm sure the priskp score said NO. Which at the end of a market should matter most"; "Yes do it"): in the last 60 s the chance of losing comes from the settlement window (the seconds banked plus the spot now, against the random walk left in it), not from the trailing minute over the whole time left."""
    from types import SimpleNamespace

    from kalshi_moonshot import facts

    read = {60: (0.50, 0.10), 5: (-0.40, 0.10)}  # the trailing minute is half a point over the target; the last 5 s are 0.4 UNDER it

    def host(banked):
        return SimpleNamespace(_wave_spot=lambda t, now, avg_seconds=60: read.get(avg_seconds, banked))

    t = SimpleNamespace(entry=0.80, wave_cushion=False)
    out = facts.Board(host((0.30, 0.10)), t, 1000.0, 90.0, 0.90)  # 90 s left: outside the window, the old reading
    assert out.get("p_lose") < 0.01 and facts.settle_end(out) is None
    b = facts.Board(host((0.30, 0.10)), t, 1000.0, 20.0, 0.90)  # 40 s banked at +0.30, 20 s to run at -0.40: ends (0.30 x 40 - 0.40 x 20) / 60 = +0.067
    ends, sd = facts.settle_end(b)
    assert abs(ends - (0.30 * 40 - 0.40 * 20) / 60) < 1e-9 and abs(sd - (20 / 60) * (0.10 / 60 ** 0.5) * (20 / 3) ** 0.5) < 1e-9
    assert b.get("p_lose") < 0.01  # banked far enough ahead that 20 s cannot undo it
    b2 = facts.Board(host((0.10, 0.10)), t, 1000.0, 40.0, 0.90)  # 20 s banked at +0.10, 40 s to run at -0.40: ends -0.233: the bet is losing, whatever the trailing minute says
    assert b2.get("p_lose") > 0.99
    b3 = facts.Board(SimpleNamespace(_wave_spot=lambda t, now, avg_seconds=60: None if avg_seconds == 5 else (0.5, 0.1)), t, 1000.0, 40.0, 0.90)
    assert facts.settle_end(b3) is None and b3.get("p_lose") < 0.01  # no 5 s reading: the old reading stands
