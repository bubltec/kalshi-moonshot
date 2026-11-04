"""Every tape in tests/replay_tapes is replayed through the real watcher and its outcome is compared with a golden file.

Add an incident:   python -m kalshi_moonshot.replay capture TICKER LANE --name NAME --note "what happened" --out tests/replay_tapes/NAME.json
                   (on the host, then copy the file here), then  pytest tests/test_replay_incidents.py --force-regen  and read the new golden.
Change a rule:     edit tests/replay_tapes/lanes.toml or the code; a changed outcome fails the test, and --force-regen accepts it once reviewed.
"""

from dataclasses import asdict
from pathlib import Path

import pytest

from kalshi_moonshot.replay import Tape, run

DIR = Path(__file__).parent / "replay_tapes"
LANES = (DIR / "lanes.toml").read_text()


@pytest.mark.parametrize("path", sorted(DIR.glob("*.json")), ids=lambda p: p.stem)
def test_replay_outcome(path, data_regression):
    o = run(Tape.load(path), LANES)
    assert not o.stuck, f"the watcher raised {o.signals} sale signals and nothing sold"
    data_regression.check({k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(o).items()})
