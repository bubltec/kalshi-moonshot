from pathlib import Path

from kalshi_moonshot import config, lanes

EXAMPLE = Path(__file__).resolve().parent.parent / "strategies.example.toml"


def test_the_example_lane_loads_and_buys_and_sells_only_through_modules():
    s = config.load()
    spec = lanes.lane_specs_from(s, EXAMPLE.read_text())["example"]
    assert spec.buy_module == "favourite" and spec.buy_chain == ("price", "spread", "entry_window", "vol_regime", "cushion", "price_trend", "spike")
    assert spec.exit_module == "hold_and_trail" and spec.sell_chain == ("under_even", "trail")
    assert spec.max_vol_rel == 1.6 and spec.curve_take_trail == 1.0 and spec.curve_risk_under_even == 0.1
