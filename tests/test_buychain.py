"""The buy as a chain of named steps in the lane's own order (buychain.py, lane field buy_chain): 2026-11-17, user: "ladder should not be the entry point"; "the risk score makes way more sense to go FIRST"; "you need to write new code that is modular and can fit in logic in any order"; and, on the shape: the lane's normal gates decide what is eligible, the risk model chooses yes or no, then more analysis confirms it. Unit tests on the walk; integration through a lane file into the review (runner.lane_shots) and the decision (runner.evaluate)."""
import time
import types
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import buychain, config, lanes, regime, runner, ticks
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

LANE = "[lane.ch]\nestimator='market'\nprice=[0.10,0.85]\nhours=[0.0,1.0]\nsides=['yes','no']\nfixed_stake=1.0\nseries=['KXBTC15M']\nmax_spread=0.05\nmin_volume=0\nneeds_model_edge=true\nmin_edge_room=0.12\nbuy_chain={chain}\n"
RISK_FIRST = "['side_score','price','ladder']"

riskscore = types.SimpleNamespace(live=None)  # the old risk score is archived (archive/old_single_score); these tests of the chain's mechanics use a stand-in step that reads a score the test sets
SIDE_LEVEL = 0.25


def _side_score_step(ctx, shot):
    """Test-only stand-in for the retired risk_score step: the side's risk (1 - chance) is at or under SIDE_LEVEL at every look."""
    rs = riskscore.live(shot.series or shot.ticker.split("-")[0], shot.ticker, shot.side, ctx.now + shot.hours_to_close * 3600.0, ctx.now, ctx.s.data_dir)
    if rs is None:
        return buychain.Check(False, "no score yet")
    risks = ", ".join(f"{100 * (1 - c):.0f}%" for c in rs[0])
    ok = all(1.0 - c <= SIDE_LEVEL for c in rs[0])
    return buychain.Check(ok, f"risk {risks}" if ok else f"risk {risks}, over the level")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setitem(buychain.STEPS, "side_score", _side_score_step)
    monkeypatch.setattr(lanes, "BUY_STEPS", (*lanes.BUY_STEPS, "side_score"))
    yield
    ticks.mids.clear()
    ticks.history.clear()
    buychain.DROPPED.clear()


def settings(tmp_path, monkeypatch, chain=RISK_FIRST, yes_score=0.80):
    """A lane file with the chain, and a risk score that has YES at `yes_score` (NO at one less it), held at every look."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(chain=chain))
    monkeypatch.setattr(riskscore, "live", lambda series, ticker, side, *a, **k: ([yes_score if side == "yes" else 1.0 - yes_score] * 3, 0.5, [0.5] * 3))
    return config.load()


def shot(side="yes", bid=0.79, ask=0.80):
    return LongShot("KXBTC15M-7", "t", "", bid, ask, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ch", series="KXBTC15M", side=side, event_ticker="KXBTC15M-7")


def test_unit_the_steps_are_asked_in_the_lanes_order_and_a_no_ends_the_walk(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, yes_score=0.60)  # a risk of 40% on YES: over the lane's 25%
    spec = lanes.spec_for(s, "ch")
    ctx = buychain.Ctx(s, spec, 0.95)
    trail = buychain.walk(ctx, shot(), ("side_score", "price", "ladder"))
    assert not trail.alive and [n for n, _ in trail.said] == ["side_score"]  # out at the first step: nothing after it was asked
    trail = buychain.walk(ctx, shot(), ("price", "ladder", "side_score"))
    assert not trail.alive and [n for n, c in trail.said] == ["price", "ladder", "side_score"] and [c.ok for _, c in trail.said] == [True, True, False]


def test_unit_a_step_that_needs_the_estimate_ends_the_reviews_walk_with_the_side_still_alive(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    ctx = buychain.Ctx(s, lanes.spec_for(s, "ch"), None)  # the review: no estimate yet
    trail = buychain.walk(ctx, shot(), ("side_score", "ladder", "price"))
    assert trail.alive and [n for n, _ in trail.said] == ["side_score"]  # stopped before the ladder, and before everything after it


def test_unit_a_step_that_cannot_be_read_has_not_said_yes(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    monkeypatch.setattr(riskscore, "live", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("feed down")))
    trail = buychain.walk(buychain.Ctx(s, lanes.spec_for(s, "ch"), 0.95), shot(), ("side_score", "price"))
    assert not trail.alive and "could not be read" in trail.said[0][1].why


def test_unit_a_lane_file_naming_an_unknown_step_or_a_step_twice_does_not_load(tmp_path, monkeypatch):
    for chain in ("['side_score','ladders']", "['price','price']"):
        with pytest.raises(ValueError, match="buy_chain"):
            lanes.lane_specs(settings(tmp_path, monkeypatch, chain=chain))


def test_unit_every_step_named_in_lanes_has_a_function_here():
    assert set(buychain.STEPS) == set(lanes.BUY_STEPS)


class Fixed:
    def __init__(self, p):
        self.p = p
        self.screened = 0

    def estimate(self, client, settings, shot):
        return Estimate(self.p, "fixed")

    def screen(self, shots, markets, spec, s, now):
        self.screened += 1
        return []


def decide(tmp_path, monkeypatch, worth_yes, side="yes", chain=RISK_FIRST, yes_score=0.80):
    s = settings(tmp_path, monkeypatch, chain=chain, yes_score=yes_score)
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    sh = shot() if side == "yes" else shot("no", 0.20, 0.21)
    return runner.evaluate(s, None, None, store, sh, estimators={"market": Fixed(worth_yes)})


def test_integration_risk_first_then_the_ladder_last_buys_the_side_the_score_picks_when_it_is_worth_it(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, worth_yes=0.92)
    assert out.action == "bet", out.note
    assert "[chain side_score > price > ladder |" in out.note and "YES passed every step" in out.note
    assert "NO out at side_score" in out.note  # the other side was asked too, and went out at the first step


def test_integration_the_ladder_has_the_last_word_and_says_so(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, worth_yes=0.81)  # the score picks YES; the ladder has it worth what it costs
    assert out.action == "pass" and "YES out at ladder" in out.note and "side_score yes" in out.note


def test_integration_a_side_the_score_does_not_pick_is_never_asked_about_its_worth(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, worth_yes=0.05, side="no")  # the ladder has NO worth 0.95 for 0.21; the score has NO at a risk of 80%
    assert out.action == "pass" and "NO out at side_score" in out.note
    no_part = out.note[out.note.index("NO out at side_score"):].split("]")[0]
    assert "ladder" not in no_part  # the ladder was not asked about a side the first step turned down


def test_integration_the_same_steps_in_another_order_stop_at_a_different_one(tmp_path, monkeypatch):
    first = decide(tmp_path, monkeypatch, worth_yes=0.81, yes_score=0.60)  # both would say no
    assert "YES out at side_score" in first.note
    (tmp_path / "b").mkdir()
    second = decide(tmp_path / "b", monkeypatch, worth_yes=0.81, chain="['ladder','price','side_score']", yes_score=0.60)
    assert "YES out at ladder" in second.note and first.action == second.action == "pass"


def test_integration_the_chain_replaces_the_lanes_older_rules_so_none_is_asked_twice(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, worth_yes=0.92, yes_score=0.78)  # the score's chance 0.78 is under the 0.80 ask and its fee: the older risk score rule (side_score_edge = 0) would refuse it
    assert out.action == "bet", out.note
    assert "the score's chance" not in out.note and "edge screen" not in out.note


def test_integration_a_lane_with_no_chain_decides_as_it_always_has(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, worth_yes=0.92, chain="[]", yes_score=0.78)
    assert out.action == "bet" and "[chain" not in out.note  # the older risk score rule, which has no cost check since side_score_edge went (a chain lane has the ladder step for the worth)


def market(now, yes_bid=0.79, yes_ask=0.80):
    return {"ticker": "KXBTC15M-7", "event_ticker": "KXBTC15M-7", "series_ticker": "KXBTC15M", "status": "active", "market_type": "binary", "yes_bid_dollars": str(yes_bid), "yes_ask_dollars": str(yes_ask), "no_bid_dollars": str(round(1 - yes_ask, 2)), "no_ask_dollars": str(round(1 - yes_bid, 2)), "volume_fp": "100", "close_time": (now + timedelta(seconds=400)).isoformat(), "title": "t"}


def test_integration_in_the_review_the_first_step_puts_the_side_forward_and_the_estimators_screen_is_not_run(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)  # the score favours YES
    spec = lanes.spec_for(s, "ch")
    est = Fixed(0.05)  # the ladder would rate NO, the other side
    now = datetime.now(UTC)
    shots = runner.lane_shots(s, None, [market(now)], spec, {"market": est}, now)
    assert est.screened == 0  # the ladder did not choose
    assert [x.side for x in shots] == ["yes"]
    dropped = buychain.DROPPED["ch"]
    assert [d["side"] for d in dropped] == ["no"] and "out at side_score" in dropped[0]["reason"]


def test_integration_in_the_review_a_lane_with_no_chain_still_goes_through_the_estimators_screen(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, chain="[]")
    est = Fixed(0.05)
    now = datetime.now(UTC)
    runner.lane_shots(s, None, [market(now)], lanes.spec_for(s, "ch"), {"market": est}, now)
    assert est.screened == 1


# Modules: a named chain in strategies.toml that lanes are assigned to (2026-11-17, user: "we should be able to have multiple modules. And assign a lane to a module").
BASE = "estimator='market'\nprice=[0.10,0.85]\nhours=[0.0,1.0]\nsides=['yes','no']\nfixed_stake=1.0\nseries=['KXBTC15M']\nmax_spread=0.05\nmin_volume=0\nneeds_model_edge=true\nmin_edge_room=0.12\n"
MODULES = "[module.risk_first]\nlabel='the risk score picks the side, the ladder checks the worth last'\nsteps=['side_score','price','ladder']\nmax_open=7\n\n[module.ladder_first]\nsteps=['ladder','price']\n\n"


def load(tmp_path, monkeypatch, text):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(text)
    s = config.load()
    return s, lanes.lane_specs(s)


def test_unit_lanes_are_assigned_to_modules_and_take_their_steps(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, MODULES + f"[lane.a]\n{BASE}buy_module='risk_first'\n\n[lane.b]\n{BASE}buy_module='ladder_first'\n\n[lane.c]\n{BASE}buy_module='risk_first'\n\n[lane.d]\n{BASE}")
    assert got["a"].buy_chain == ("side_score", "price", "ladder") and got["a"].buy_module == "risk_first"
    assert got["b"].buy_chain == ("ladder", "price") and got["b"].buy_module == "ladder_first"
    assert got["c"].buy_chain == got["a"].buy_chain  # two lanes on one module
    assert got["d"].buy_chain == () and got["d"].buy_module == ""  # a lane with no module decides as it always has


def test_unit_a_modules_settings_are_the_lanes_unless_the_lane_writes_its_own(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, MODULES + f"[lane.a]\n{BASE}buy_module='risk_first'\n\n[lane.b]\n{BASE}buy_module='risk_first'\nmax_open=9\n")
    assert got["a"].max_open == 7 and got["b"].max_open == 9


def test_unit_a_module_reaches_a_lane_through_inherit(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, MODULES + f"[lane.a]\n{BASE}buy_module='risk_first'\n\n[lane.twin]\ninherit='a'\nfixed_stake=2.0\n")
    assert got["twin"].buy_chain == ("side_score", "price", "ladder") and got["twin"].buy_module == "risk_first"


@pytest.mark.parametrize("text,word", [
    (MODULES + f"[lane.a]\n{BASE}buy_module='nope'\n", "not a module"),
    (MODULES + f"[lane.a]\n{BASE}buy_module='risk_first'\nbuy_chain=['price']\n", "writes buy_chain too"),
    (f"[module.empty]\nlabel='x'\n\n[lane.a]\n{BASE}buy_module='empty'\n", "needs `steps`"),
    (f"[module.bad]\nsteps=['side_score','ladders']\n\n[lane.a]\n{BASE}buy_module='bad'\n", "buy_chain names"),
    (f"[module.bad]\nsteps=['price']\nnot_a_setting=1\n\n[lane.a]\n{BASE}buy_module='bad'\n", "unknown field"),
    (f"[module.bad]\nsteps=['price']\ninherit='a'\n\n[lane.a]\n{BASE}buy_module='bad'\n", "does not name"),
])
def test_unit_a_lane_file_with_a_bad_module_does_not_load(tmp_path, monkeypatch, text, word):
    with pytest.raises(ValueError, match=word):
        load(tmp_path, monkeypatch, text)


def test_unit_the_modules_name_is_not_what_the_lane_does_its_steps_are(tmp_path, monkeypatch):
    s, got = load(tmp_path, monkeypatch, MODULES + "[module.same_steps]\nsteps=['side_score','price','ladder']\nmax_open=7\n\n" + f"[lane.a]\n{BASE}buy_module='risk_first'\n\n[lane.b]\n{BASE}buy_module='same_steps'\n\n[lane.c]\n{BASE}buy_module='ladder_first'\n")
    va, vb, vc = (lanes.settings_of(got[n], s) for n in "abc")
    assert va == vb and va != vc


def test_integration_a_lane_on_a_module_buys_by_its_steps_and_the_note_names_the_module(tmp_path, monkeypatch):
    monkeypatch.setattr(riskscore, "live", lambda series, ticker, side, *a, **k: ([0.80 if side == "yes" else 0.20] * 3, 0.5, [0.5] * 3))
    s, _ = load(tmp_path, monkeypatch, MODULES + f"[lane.ch]\n{BASE}buy_module='risk_first'\n")
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    out = runner.evaluate(s, None, None, store, shot(), estimators={"market": Fixed(0.92)})
    assert out.action == "bet" and "[chain risk_first: side_score > price > ladder |" in out.note


def test_unit_the_state_record_shadow_step_never_says_no_and_notes_the_verdict(tmp_path, monkeypatch):
    from kalshi_moonshot import staterecord

    s = settings(tmp_path, monkeypatch)
    spec = lanes.spec_for(s, "ch")
    ctx = buychain.Ctx(s, spec, 0.95)
    monkeypatch.setattr(staterecord, "admits", lambda *a, **k: (False, "state DF, record -0.10 per $1 over 90 markets"))
    got = buychain.STEPS["state_record_shadow"](ctx, shot())
    assert got.ok and "would not buy" in got.why and "state DF" in got.why
    monkeypatch.setattr(staterecord, "admits", lambda *a, **k: (True, "state EF, record +0.30 per $1 over 90 markets"))
    got = buychain.STEPS["state_record_shadow"](ctx, shot())
    assert got.ok and "would buy" in got.why and "would not" not in got.why

    def broken(*a, **k):
        raise RuntimeError("no record")

    monkeypatch.setattr(staterecord, "admits", broken)
    assert buychain.STEPS["state_record_shadow"](ctx, shot()).ok
    base = lanes.settings_of(spec, s)["buy_chain"]
    (tmp_path / "strategies.toml").write_text(LANE.format(chain="['side_score','price','state_record_shadow']"))
    s2 = config.load()
    assert "state_record_shadow" not in lanes.settings_of(lanes.spec_for(s2, "ch"), s2)["buy_chain"] and "side_score" in base  # a shadow step is not part of the lane's version
