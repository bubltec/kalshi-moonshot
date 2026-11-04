"""Replays of recorded markets through the real watcher.

A study script that re-implements a rule drifts from the live code: the 2026-11-03 shock and wrong-side exits replayed well and then never sold,
because the live sale path vetoed them and the replays never ran that path. Here a recorded `Tape` (the quotes and the index ticks of one held
bet) is fed event by event to a real `ProfitWatcher`, and its sale signals go through the real `_sell`, so a replay can only differ from the live
bot by its data.

    tape.py     Tape: one held bet with the quotes and index ticks around its close; capture from the databases, save and load as JSON
    harness.py  run(tape, lanes_toml) -> Outcome: what the watcher did with the tape and what that was worth against holding
    __main__.py python -m kalshi_moonshot.replay capture|run|list

Incident tapes live in tests/replay_tapes/ and are asserted in tests/test_replay_incidents.py.
"""

from .harness import Outcome, run
from .tape import Tape

__all__ = ["Outcome", "Tape", "run"]
