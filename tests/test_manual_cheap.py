from kalshi_moonshot import config, watcher


def settings():
    return config.load()


def cheap_target(entry=0.10, s=None):
    s = s or settings()
    t = watcher.Target("KXBTCD-26OCT2415-T80000", "manual", "yes", entry, 0.0, 0.0, 0.0, 30.0, 0.10, s.manual_exit_activate, s.manual_exit_drop, 10.0, s.manual_exit_fast, 3.0, entry)
    watcher.ProfitWatcher._cheap_rules(s, t)
    return t


def feed(t, series, check_at=None):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    w.targets[("KXBTCD-26OCT2415-T80000", "manual")] = t
    for ts, bid in series:
        w.on_event({"kind": "quote", "ticker": "KXBTCD-26OCT2415-T80000", "bid": bid, "ask": bid + 0.01, "ts": ts})
    if check_at:
        w.check_timers(check_at)
    return w


def test_a_cheap_buy_is_held_with_no_take_profit_trail_or_stop():
    t = cheap_target(0.10)
    assert t.x == 0.0 and t.trail_activate == 0.0 and t.trail_drop == 0.0 and t.trail_fast == 0.0 and t.trail_giveback == 0.0
    assert t.stop_drop == 0.0 and t.stop_since is None


def test_a_1_to_20c_moonshot_is_not_sold_on_a_bounce():
    w = feed(cheap_target(0.05), [(1000.0, 0.06), (1001.0, 0.18), (1002.0, 0.30), (1010.0, 0.12)], check_at=1020.0)
    assert w._jobs.empty()


def test_a_cheap_buy_that_never_rose_just_rides():
    w = feed(cheap_target(0.10), [(1000.0, 0.09), (1005.0, 0.05), (1010.0, 0.02), (1100.0, 0.01)], check_at=1200.0)
    assert w._jobs.empty()


def test_optional_take_x_still_sells_when_you_turn_it_on(monkeypatch):
    monkeypatch.setenv("MANUAL_CHEAP_TAKE_X", "3")
    s = config.load()
    t = cheap_target(0.10, s)
    assert t.x == 3.0
    w = feed(t, [(1000.0, 0.12), (1001.0, 0.20), (1002.0, 0.30)])
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("take-profit")


def test_a_dearer_position_keeps_the_general_hand_placed_rules_and_follows_the_average_price():
    s = settings()
    t = cheap_target(0.60, s)
    assert t.x == 0.0 and t.trail_activate == s.manual_exit_activate
    t.entry = 0.10  # you add to the position and the average drops into the cheap range
    watcher.ProfitWatcher._cheap_rules(s, t)
    assert t.x == 0.0 and t.trail_activate == 0.0
    t.entry = 0.60  # and back out
    watcher.ProfitWatcher._cheap_rules(s, t)
    assert t.x == 0.0 and t.trail_activate == s.manual_exit_activate


def test_the_defaults_hold_a_moonshot():
    s = settings()
    assert (s.manual_cheap_max, s.manual_cheap_take_x, s.manual_cheap_arm_x, s.manual_cheap_giveback) == (0.25, 0.0, 0.0, 0.0)


def test_a_cheap_buy_has_no_stop_even_when_the_hand_placed_stop_is_on_but_a_dearer_one_keeps_it():
    s = settings()
    cheap = watcher.Target("KXBTCD-26OCT2415-T80000", "manual", "yes", 0.10, 0.0, 0.0, 0.0, 30.0, 0.10, s.manual_exit_activate, s.manual_exit_drop, 10.0, s.manual_exit_fast, 3.0, 0.10)
    dear = watcher.Target("KXBTCD-26OCT2415-T80000", "manual", "yes", 0.50, 0.0, 0.0, 0.0, 30.0, 0.10, s.manual_exit_activate, s.manual_exit_drop, 10.0, s.manual_exit_fast, 3.0, 0.50)
    for t in (cheap, dear):
        t.stop_drop, t.stop_since = 0.30, 123.0  # as _arm_stop leaves it with MANUAL_EXIT_STOP_LOSS=0.30
        watcher.ProfitWatcher._cheap_rules(s, t)
    assert cheap.stop_drop == 0.0 and cheap.stop_since is None
    assert dear.stop_drop == 0.30 and dear.stop_since == 123.0
