from kalshi_moonshot import config, lanes
from kalshi_moonshot.estimators import MarketEstimator
from kalshi_moonshot.strategy import LongShot


def shot(side, ask):
    return LongShot(ticker="KXINXU-1", title="", rules="", bid=ask - 0.01, ask=ask, volume=0.0, close_time="", hours_to_close=1.0,
                    series="KXINXU", event_ticker="KXINXU-E", side=side)


def test_the_market_estimator_has_no_opinion():
    est = MarketEstimator()
    assert est.estimate(None, None, shot("yes", 0.80)).p == 0.80  # chance YES = the price asked
    assert abs(est.estimate(None, None, shot("no", 0.80)).p - 0.20) < 1e-9  # a NO ask of 0.80 means YES at 0.20


def test_the_index_favorite_lane_is_tiered_and_data_driven():
    s = config.load()
    spec = lanes.lane_specs(s).get("index_favorite")
    if spec is None:
        return  # data/strategies.toml is the user's; the lane may not be present in a test checkout
    assert spec.estimator == lanes.MARKET and lanes.tiered(spec) and spec.series
