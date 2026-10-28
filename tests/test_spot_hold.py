from kalshi_moonshot import config, lanes, watcher


def target(sigma=0.5):
    t = watcher.Target("KXBTC15M-1", "fav", "yes", 0.86, 0.0, 0.0, 0.0, 30.0, 0.10, 0.10, 0.04, 10.0, 0.12, 3.0, 0.86)
    t.spot_sigma = sigma
    return t


def watcher_with(p):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None, spot_side=lambda tk, side: p)
    t = target()
    w.targets[("KXBTC15M-1", "fav")] = t
    return w, t


CRASH = "peak-crash: bid 0.86 is 0.10 off its 0.96 peak (paid 0.86) for 10s"


def test_it_waits_while_spot_is_comfortably_on_the_winning_side():
    w, t = watcher_with(0.88)  # well past 0.5 sigma (0.69)
    assert w._spot_holds(t, "KXBTC15M-1", "yes", CRASH) is True
    assert t.retry_after > 0  # it looks again later


def test_it_sells_when_spot_has_crossed_or_is_within_the_margin():
    for p in (0.31, 0.50, 0.69):
        w, t = watcher_with(p)
        assert w._spot_holds(t, "KXBTC15M-1", "yes", CRASH) is False


def test_a_stop_is_never_held_back_by_spot():
    w, t = watcher_with(0.95)
    assert w._spot_holds(t, "KXBTC15M-1", "yes", "stop-loss: bid 0.30 is 65% under the 0.86 paid") is False


def test_unknown_spot_or_a_failing_lookup_does_not_block_the_exit():
    w, t = watcher_with(None)
    assert w._spot_holds(t, "KXBTC15M-1", "yes", CRASH) is False

    def boom(tk, side):
        raise RuntimeError("no feed")

    w2 = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None, spot_side=boom)
    assert w2._spot_holds(t, "KXBTC15M-1", "yes", CRASH) is False


def test_off_means_off():
    w, t = watcher_with(0.99)
    t.spot_sigma = 0.0
    assert w._spot_holds(t, "KXBTC15M-1", "yes", CRASH) is False


def test_default_is_half_a_sigma_and_follows_the_lane(monkeypatch):
    monkeypatch.delenv("CRASH_SPOT_SIGMA")
    s = config.load()
    assert s.crash_spot_sigma == 0.5
    spec = lanes.lane_specs(s)["commodity"]
    assert spec.trail_spot_sigma is None
    assert lanes.settings_of(spec, s)["crash_spot_sigma"] == "0.5"


BURST = "burst: $623 bought against the yes holder in 3 s (bar $400), the favorite's bid 0.51; the no side costs 0.49"


def burst_watcher(p, edge=0.05):
    w, t = watcher_with(p)
    t.burst_spot_edge = edge
    return w, t


def test_a_burst_is_held_when_spot_says_the_side_is_worth_more_than_the_sale():
    w, t = burst_watcher(0.70)  # sale 0.49 less its fee, chance 0.70
    assert w._spot_holds(t, "KXBTC15M-1", "yes", BURST, 0.49) is True
    assert t.burst_resell is True  # the next burst may still sell it
    assert t.retry_after > 0


def test_a_burst_sells_when_spot_agrees_with_the_bid_or_has_no_opinion():
    for p in (0.20, 0.49, 0.51, None):
        w, t = burst_watcher(p)
        assert w._spot_holds(t, "KXBTC15M-1", "yes", BURST, 0.49) is False


def test_a_burst_is_unchanged_when_the_lane_leaves_the_edge_off():
    w, t = burst_watcher(0.95, edge=0.0)
    assert w._spot_holds(t, "KXBTC15M-1", "yes", BURST, 0.49) is False


def test_a_failing_spot_lookup_does_not_block_a_burst_sale():
    def boom(tk, side):
        raise RuntimeError("no feed")

    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None, spot_side=boom)
    t = target()
    t.burst_spot_edge = 0.05
    assert w._spot_holds(t, "KXBTC15M-1", "yes", BURST, 0.49) is False
