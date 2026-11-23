"""A decision's stored reason says why first (2026-11-17, user: "my only issue is the number of buys. are we flagging why we turn things down?")."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import decide_support as T

from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store

VIEWS = "[crossed 8] [spike view z -0.4, 74 of 120 as large] [scenario: target low in the 60 min, cushion against | target in range 15:0.19 30:0.11 60:0.08 | drift 15:-1.0 30:-1.3 60:-0.7 | cushion -0.67 | spot in range now 15:0.78 30:0.43 60:0.34 | last markets margin/flips -0.7/8 -0.6/5 +0.1/8 -0.3/5]"


def test_unit_the_verdicts_come_first_in_their_order_and_the_readings_after():
    note = f"{VIEWS} same side held: 2 of this lane's bets are already NO. [cushion -0.7, chance 0.300] the edge -0.045 is under 8% of the room."
    assert len(VIEWS) > 290 and "same side held" not in note[:300]  # the readings alone filled the 300 characters that used to be stored
    got = runner.stored_reason(note)
    assert got.startswith("same side held: 2 of this lane's bets are already NO. the edge -0.045 is under 8% of the room. [crossed 8] [spike view")
    assert got.endswith("[cushion -0.7, chance 0.300]") and len(got) <= runner.REASON_MAX


def test_unit_the_whole_note_is_stored_and_only_a_runaway_one_is_bounded_from_the_readings_end():
    note = f"{VIEWS} {VIEWS} {VIEWS} the lane holds at most 2 on a side."  # about 900 characters: an ordinary long note
    got = runner.stored_reason(note)
    assert got.startswith("the lane holds at most 2 on a side. [crossed 8]") and got.count("[scenario") == 3 and len(got) == len(" ".join(note.split()))  # nothing is dropped
    runaway = runner.stored_reason(" ".join([VIEWS] * 30) + " the verdict.")
    assert runaway.startswith("the verdict. ") and len(runaway) == runner.REASON_MAX


def test_unit_the_edge_screen_says_why_in_words_and_says_nothing_when_it_lets_the_buy_through(tmp_path, monkeypatch):
    from kalshi_moonshot import lanes

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.e]\nestimator='ladder'\nprice=[0.1,0.9]\nhours=[0.0,1.0]\nmin_edge_room=0.08\n[lane.f]\nestimator='ladder'\nprice=[0.1,0.9]\nhours=[0.0,1.0]\nneeds_model_edge=false\n")
    s = config.load()
    e, f = lanes.lane_specs(s)["e"], lanes.lane_specs(s)["f"]
    under = lanes.edge_verdict(e, 0.70, 0.85, -0.159)  # XRP 13:50:41 on 2026-11-17: recorded as a bare "price band"
    assert under.startswith("edge screen: the model's chance 0.70 is under what the side costs (0.85 and its fee)") and "-0.159" in under
    short = lanes.edge_verdict(e, 0.64, 0.60, 0.026)  # BTC 13:50:31: an edge, but under 8% of the room
    assert short.startswith("edge screen: the edge +0.026") and "+0.031" in short and "8% of the room" in short
    assert lanes.edge_verdict(e, 0.75, 0.64, 0.092) is None  # over the bar: the screen has nothing to say
    assert lanes.edge_verdict(f, 0.10, 0.60, -0.5) is None  # a lane that asks for no edge
    for p, ask, edge in ((0.70, 0.85, -0.159), (0.64, 0.60, 0.026), (0.75, 0.64, 0.092), (0.5, 0.5, 0.0)):
        assert (lanes.edge_verdict(e, p, ask, edge) is None) == lanes.wants_bet(e, p, ask, edge)  # a sentence exactly when the screen says no


def test_integration_a_buy_turned_down_at_the_edge_screen_is_stored_saying_so(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "LANE", T.LANE.replace("always=true\n", ""))  # a lane that needs an edge from its model
    out = T.decide(tmp_path, monkeypatch, time.time(), "min_edge_room=0.9", ask=0.81)  # the test estimator says 0.90: nowhere near 90% of the room
    assert out.action == "pass"
    row = Store(config.load().db_path).db.execute("SELECT reason FROM decisions WHERE lane='rs'").fetchone()
    assert row["reason"].startswith("edge screen: the edge +") and "90% of the room" in row["reason"]
