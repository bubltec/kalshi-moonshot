from pathlib import Path

from kalshi_moonshot import config, lanes

EXAMPLE = Path(__file__).resolve().parent.parent / "strategies.example.toml"


def test_the_late_ticket_lane_gates_on_the_estimators_chance():
    spec = lanes.lane_specs_from(config.load(), EXAMPLE.read_text())["late_ticket"]
    assert spec.always and spec.fixed_stake == 1.0 and lanes.tiered(spec)
    assert lanes.chance_ok(spec, 0.45) and lanes.chance_ok(spec, 0.40) and lanes.chance_ok(spec, 0.50)
    assert not lanes.chance_ok(spec, 0.12) and not lanes.chance_ok(spec, 0.75)


def test_lanes_without_a_chance_band_accept_any_chance():
    spec = lanes.builtin_lanes(config.load())["steady"]
    assert lanes.chance_ok(spec, 0.01) and lanes.chance_ok(spec, 0.99)


def test_the_example_15_minute_lanes_accept_price_threshold_markets():
    specs = lanes.lane_specs_from(config.load(), EXAMPLE.read_text())
    assert specs["late_ticket"].numeric == "allow" and specs["fifteen"].numeric == "allow"
