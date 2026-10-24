from kalshi_moonshot import config, lanes, watcher


def target(**kw):
    t = watcher.Target("KXBTC15M-1", "fav", "yes", 0.60, 0.0, 0.0, 0.0, 30.0, 0.10, 0.0, 0.0, 10.0, 0.0, 3.0, 0.60)
    t.hover_dip = 0.50
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def feed(t, series):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    w.targets[("KXBTC15M-1", "fav")] = t
    for ts, bid in series:
        w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": bid + 0.01, "ts": ts})
    return w


def test_it_sells_at_the_first_profit_after_the_bid_dipped_to_the_strike():
    w = feed(target(), [(1000.0, 0.58), (1010.0, 0.49), (1020.0, 0.55), (1030.0, 0.66)])
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("hover: bid dipped to 0.49")


def test_it_does_not_sell_a_profit_that_never_dipped():
    w = feed(target(), [(1000.0, 0.62), (1010.0, 0.66), (1020.0, 0.70)])
    assert w._jobs.empty()


def test_it_does_not_sell_while_still_under_the_cost_after_dipping():
    w = feed(target(), [(1000.0, 0.48), (1010.0, 0.52), (1020.0, 0.60)])
    assert w._jobs.empty()


def test_it_is_off_by_default_in_every_lane_but_the_hover_twin():
    s = config.load()
    assert all(spec.hover_dip == 0.0 for spec in lanes.lane_specs(s).values() if spec.name != "fx_favorite_hover")
