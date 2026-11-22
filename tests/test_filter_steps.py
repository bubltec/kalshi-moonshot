"""The lane filter's judgements are buy steps (2026-11-21, user: "I keep finding out about new things influencing the buys and sells that I didn't ask for"; "it should be a standard for the module").

For a lane that buys by a module, the price range, spread, entry window, time left, blackout, volume and least return are steps of the module and nothing else applies them. The first test holds the move to "nothing changed": over a grid of markets, the old filter's yes is the same as the new filter's yes plus the steps' yes. The second holds the live lane file to listing a step for every bound it sets."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_moonshot import buychain, config
from kalshi_moonshot.lanes import lane_specs, lane_specs_from, spec_for
from kalshi_moonshot.strategy import lane_candidates, lane_reject_reason, side_view

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)  # a Friday, 10:00 ET
FILTER_STEPS = ("blackout", "price", "min_return", "spread", "volume", "entry_window", "time_left")


def market(ask: float, bid: float, minutes_left: float, volume: float = 50.0, series: str = "KXBTC15M") -> dict:
    return {
        "ticker": f"{series}-T", "event_ticker": f"{series}-E", "series_ticker": series, "status": "active", "market_type": "binary",
        "yes_ask_dollars": f"{ask:.4f}", "yes_bid_dollars": f"{bid:.4f}", "volume_fp": str(volume),
        "close_time": (NOW + timedelta(minutes=minutes_left)).isoformat(),
    }


def lane(**over):
    s = config.load()
    base = spec_for(s, "commodity")
    spec = replace(
        base, name="t", series=("KXBTC15M",), needs_instrument=False, sides=("yes", "no"), price=(0.2, 0.6), max_spread=0.03, min_volume=10.0,
        min_return=0.2, hours=(0.02, 0.26), entry_elapsed=(3.0, 11.0), timed_suffix="15M", timed_minutes=15.0, min_age_minutes=0.0,
        blackout_et=("09:00-09:30",), numeric="allow", buy_chain=(), **over,
    )
    return s, spec


def grid():
    for ask in (0.10, 0.20, 0.35, 0.60, 0.75):
        for gap in (0.01, 0.03, 0.05):
            for left in (0.5, 2.0, 5.0, 11.5, 13.0):
                for volume in (0.0, 50.0):
                    yield market(ask, ask - gap, left, volume)


def test_the_steps_say_exactly_what_the_filter_said():
    s, old = lane()
    new = replace(old, buy_chain=FILTER_STEPS)
    seen = {True: 0, False: 0}
    for when in (NOW, NOW.replace(hour=14, minute=10)):  # the second is inside the blackout (09:10 ET)
        for m in grid():
            m = {**m, "close_time": (when + (datetime.fromisoformat(m["close_time"]) - NOW)).isoformat()}
            for side in ("yes", "no"):
                before = lane_reject_reason(side_view(m, side), old, s, when) is None
                cands = [x for x in lane_candidates([m], new, s, when) if x.side == side]
                after = bool(cands) and bool(buychain.screen(buychain.Ctx(s, new, None, when.timestamp()), cands))
                assert before == after, (m, side, when)
                seen[before] += 1
    assert seen[True] > 5 and seen[False] > 50  # the grid has both answers


def test_a_step_the_module_does_not_list_is_not_applied_and_the_scope_keeps_its_bounds():
    s, old = lane()
    wide = market(0.35, 0.25, 5.0)  # a 10c spread
    assert lane_reject_reason(wide, old, s, NOW) == "spread too wide"
    no_spread = replace(old, buy_chain=tuple(x for x in FILTER_STEPS if x != "spread"))
    cands = lane_candidates([wide], no_spread, s, NOW)
    assert [x.side for x in buychain.screen(buychain.Ctx(s, no_spread, None, NOW.timestamp()), cands)] == ["yes"]  # (NO at 0.75 is out at price)
    assert lane_reject_reason(wide, no_spread, s, NOW, scope=True) == "spread too wide"  # the watchlist pass is handed a relaxed lane and keeps what it is handed


def test_each_step_names_what_it_read():
    s, old = lane()
    spec = replace(old, buy_chain=FILTER_STEPS)
    ctx = buychain.Ctx(s, spec, None, NOW.timestamp())
    (shot,) = [x for x in lane_candidates([market(0.35, 0.25, 13.0)], spec, s, NOW) if x.side == "yes"]
    said = {name: buychain.STEPS[name](ctx, shot) for name in FILTER_STEPS}
    assert not said["spread"].ok and "0.10" in said["spread"].why
    assert not said["entry_window"].ok and "2.0 min into" in said["entry_window"].why
    assert said["time_left"].ok and said["blackout"].ok and said["volume"].ok and said["min_return"].ok and said["price"].ok


def test_the_live_lane_file_lists_a_step_for_every_bound_a_module_lane_sets():
    """A bound with no step would be a setting that does nothing: the lane file must say so by listing the step or dropping the bound."""
    path = Path("data/strategies.toml")
    for text in ([path.read_text()] if path.exists() else []) + [Path("strategies.example.toml").read_text()]:
        s = config.load()
        for name, sp in lane_specs_from(s, text).items():
            if not sp.buy_chain or not sp.enabled:
                continue
            want = {"price"}
            if sp.max_spread < 1.0:
                want.add("spread")
            if sp.timed_suffix and (sp.entry_elapsed[0] > 0 or sp.entry_elapsed[1] < sp.timed_minutes):
                want.add("entry_window")
            if sp.hours[0] > 0 or (sp.timed_suffix and sp.hours[1] < sp.timed_minutes / 60.0):
                want.add("time_left")
            if sp.blackout_et:
                want.add("blackout")
            if sp.min_volume > 0:
                want.add("volume")
            if sp.min_return > 0:
                want.add("min_return")
            assert want <= set(sp.buy_chain), f"{name}: sets a bound with no step: {sorted(want - set(sp.buy_chain))}"
    assert lane_specs  # (imported for the running bot's own reader; the texts above are read directly)


def test_the_socket_trigger_holds_only_the_bounds_the_module_lists():
    from kalshi_moonshot import entrywatch

    s, old = lane()
    close = (NOW + timedelta(minutes=14)).isoformat()  # one minute in: before the entry window
    assert not entrywatch.in_window(old, close, NOW, "KXBTC15M")
    assert not entrywatch.in_window(replace(old, buy_chain=("price", "entry_window")), close, NOW, "KXBTC15M")
    assert entrywatch.in_window(replace(old, buy_chain=("price",)), close, NOW, "KXBTC15M")
    assert not entrywatch.in_band(old, 0.90, 0.92)
    assert not entrywatch.in_band(replace(old, buy_chain=("price",)), 0.90, 0.92)
    assert entrywatch.in_band(replace(old, buy_chain=("spread",)), 0.90, 0.92)
    inside = NOW.replace(hour=14, minute=10)  # 09:10 ET, in the lane's blackout
    close = (inside + timedelta(minutes=8)).isoformat()
    assert not entrywatch.in_window(replace(old, buy_chain=("blackout", "entry_window")), close, inside, "KXBTC15M")
    assert entrywatch.in_window(replace(old, buy_chain=("entry_window",)), close, inside, "KXBTC15M")
