
from kalshi_moonshot import staterecord
from kalshi_moonshot.estimators import Estimate


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def test_a_reading_counts_once_per_market_side_state_and_band_and_the_edge_is_shrunk():
    cells: dict = {}
    seen: set = set()
    for _ in range(3):  # the same market side and state read at three moments is one reading
        staterecord.add_reading(cells, seen, "M1", "yes", "DF", 0.40, 0.50)
    staterecord.add_reading(cells, seen, "M2", "yes", "DF", 0.41, -1.0)
    staterecord.add_reading(cells, seen, "M2", "yes", "DF", 0.80, 0.1)  # another price band is its own cell
    assert cells["DF|1"] == [2, -0.5] and cells["DF|2"] == [1, 0.1]
    n, net = staterecord.edge({"cells": {"DF|1": [30, 9.0]}}, "DF", 0.45)
    assert n == 30 and abs(net - 9.0 / 40.0) < 1e-9
    assert staterecord.edge({"cells": {}}, "DF", 0.45) == (0, 0.0)


def test_state_key_needs_every_scale():
    assert staterecord.state_key({"5m": "D", "60m": "F"}) == "DF"
    assert staterecord.state_key({"5m": "D"}) is None

