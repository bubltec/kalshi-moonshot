from kalshi_moonshot import config, runner


class FakeClient:
    def __init__(self):
        self.window = None

    def event_info(self):
        return {"EV-SPORT": ("Sports", "KXSPORT"), "EV-POL": ("Politics", "KXPOL")}

    def markets_closing(self, min_ts, max_ts):
        self.window = (min_ts, max_ts)
        yield {"ticker": "A", "event_ticker": "EV-SPORT"}
        yield {"ticker": "B", "event_ticker": "EV-POL"}
        yield {"ticker": "C", "event_ticker": "EV-UNKNOWN"}


def settings(**over):
    s = config.load()
    return type(s)(**{**s.__dict__, **over})


def test_markets_get_a_category_and_series():
    s = settings(skip_categories=frozenset())
    got = {m["ticker"]: m for m in runner.candidate_markets(s, FakeClient())}
    assert got["A"]["category"] == "Sports"
    assert got["B"]["category"] == "Politics" and got["B"]["series_ticker"] == "KXPOL"
    assert got["C"]["category"] == ""


def test_skipped_category_is_dropped_but_unknown_is_kept():
    s = settings(skip_categories=frozenset({"Sports"}))
    tickers = {m["ticker"] for m in runner.candidate_markets(s, FakeClient())}
    assert tickers == {"B", "C"}


def test_window_covers_both_lanes():
    s = settings(
        min_hours_to_close=2.0, max_hours_to_close=24.0,
        steady_enabled=True, steady_min_hours_to_close=1.0, steady_max_hours_to_close=72.0,
        skip_categories=frozenset(),
    )
    c = FakeClient()
    list(runner.candidate_markets(s, c))
    lo, hi = c.window
    assert abs((hi - lo) / 3600 - 71.0) < 0.01  # 1h .. 72h
    s2 = settings(
        min_hours_to_close=2.0, max_hours_to_close=24.0, steady_enabled=False,
        skip_categories=frozenset(),
    )
    list(runner.candidate_markets(s2, c))
    assert abs((c.window[1] - c.window[0]) / 3600 - 22.0) < 0.01
