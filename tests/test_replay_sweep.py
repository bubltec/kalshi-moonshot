"""The sweep, the lane override writer and the strategy board."""

import tomllib
from pathlib import Path

import pytest

from kalshi_moonshot.replay import Tape, studies, sweep

ROOT = Path(__file__).parent.parent
LANES = (Path(__file__).parent / "replay_tapes" / "lanes.toml").read_text()
TAPES = [Tape.load(p) for p in sorted((Path(__file__).parent / "replay_tapes").glob("*.json"))]


def test_override_changes_one_setting_and_keeps_the_rest():
    doc = tomllib.loads(sweep.override(LANES, "commodity_energy", shock_bp=12, tags={"a": [1, 2]}))["lane"]["commodity_energy"]
    assert doc["shock_bp"] == 12 and doc["wrong_side_series"] == ["KXWTI15M"] and doc["tags"] == {"a": [1, 2]}


def test_a_sweep_has_a_row_per_tape_and_point_and_a_wider_shock_sells_less(data_regression):
    rows = sweep.sweep(TAPES, LANES, {"shock_bp": [0, 8, 100], "wrong_side_bp": [0]})
    assert len(rows) == 3 * len(TAPES)
    table = sweep.summarize(rows, ["shock_bp", "wrong_side_bp"]).reset_index()
    data_regression.check({"rows": table.to_dict(orient="records")})
    assert not any(r["stuck"] for r in rows)


@pytest.mark.parametrize("path", sorted((ROOT / "studies").glob("*.yaml")), ids=lambda p: p.stem)
def test_every_study_and_the_board_load(path):
    if path.name == "concepts.yaml":
        rows = studies.concepts(path)
        assert len({r["id"] for r in rows}) == len(rows)
    else:
        s = studies.load(path)
        assert s.grid and all(isinstance(v, list) and v for v in s.grid.values())
