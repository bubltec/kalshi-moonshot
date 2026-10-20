from pathlib import Path

from kalshi_moonshot import config, lanes

EXAMPLE = Path(__file__).resolve().parent.parent / "strategies.example.toml"


def test_the_example_lanes_parse_and_the_control_matches_steady():
    s = config.load()
    specs = lanes.lane_specs_from(s, EXAMPLE.read_text())
    raw, steady = specs["steady_raw"], specs["steady"]
    assert raw.always and raw.fixed_stake == 2.0
    for field in ("price", "hours", "max_spread", "min_volume", "trend", "lookback_hours", "min_trend", "sides", "one_per"):
        assert getattr(raw, field) == getattr(steady, field), field
