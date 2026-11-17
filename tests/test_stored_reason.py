"""A decision's stored reason says why first (2026-11-17, user: "my only issue is the number of buys. are we flagging why we turn things down?")."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_risk_score as T  # noqa: E402

from kalshi_moonshot import config, runner  # noqa: E402
from kalshi_moonshot.paperfill import FLOOR_MARK  # noqa: E402
from kalshi_moonshot.store import Store  # noqa: E402

VIEWS = "[crossed 8] [spike view z -0.4, 74 of 120 as large] [scenario: target low in the 60 min, cushion against | target in range 15:0.19 30:0.11 60:0.08 | drift 15:-1.0 30:-1.3 60:-0.7 | cushion -0.67 | spot in range now 15:0.78 30:0.43 60:0.34 | last markets margin/flips -0.7/8 -0.6/5 +0.1/8 -0.3/5]"


def test_unit_the_verdicts_come_first_in_their_order_and_the_readings_after():
    note = f"{VIEWS} same side held: 2 of this lane's bets are already NO. [cushion -0.7, chance 0.300] the edge -0.045 is under 8% of the room."
    assert len(VIEWS) > 290 and "same side held" not in note[:300]  # the readings alone filled the 300 characters that used to be stored
    got = runner.stored_reason(note)
    assert got.startswith("same side held: 2 of this lane's bets are already NO. the edge -0.045 is under 8% of the room. [crossed 8] [spike view")
    assert got.endswith("[cushion -0.7, chance 0.300]") and len(got) <= runner.REASON_MAX


def test_unit_a_floor_bids_mark_stays_at_the_front_and_nothing_is_lost_in_a_short_note():
    got = runner.stored_reason(f"{FLOOR_MARK} 5s: a paper bid at the floor 0.36, judged on the tape] [crossed 7] stake $20.62 (fixed stake). [stake x0.69]")
    assert got == f"{FLOOR_MARK} 5s: a paper bid at the floor 0.36, judged on the tape] stake $20.62 (fixed stake). [crossed 7] [stake x0.69]"
    assert runner.stored_reason("no brackets at all.") == "no brackets at all."
    assert runner.stored_reason("[only a reading]") == "[only a reading]"


def test_unit_the_whole_note_is_stored_and_only_a_runaway_one_is_bounded_from_the_readings_end():
    note = f"{VIEWS} {VIEWS} {VIEWS} the lane holds at most 2 on a side."  # about 900 characters: an ordinary long note
    got = runner.stored_reason(note)
    assert got.startswith("the lane holds at most 2 on a side. [crossed 8]") and got.count("[scenario") == 3 and len(got) == len(" ".join(note.split()))  # nothing is dropped
    runaway = runner.stored_reason(" ".join([VIEWS] * 30) + " the verdict.")
    assert runaway.startswith("the verdict. ") and len(runaway) == runner.REASON_MAX


def test_integration_a_pass_is_stored_with_its_reason_first(tmp_path, monkeypatch):
    out = T.decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.01\nentry_scenario_view=true\nentry_spike_view=true\nentry_crossed_view=true")
    assert out.action == "pass" and out.note.lstrip().startswith("[")  # the note in hand still leads with the readings
    row = Store(config.load().db_path).db.execute("SELECT reason FROM decisions WHERE lane='rs'").fetchone()
    assert not row["reason"].startswith("[")
    assert row["reason"].startswith("the lane buys a risk of 1% or under that has held for a minute")  # the rule that turned it down
    assert "[risk score: risk " in row["reason"] and "[scenario" in row["reason"]  # the readings are still there, after it
