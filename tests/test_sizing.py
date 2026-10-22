from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import bankroll, config, lanes, runner
from kalshi_moonshot.sizing import kelly_share, size_bet
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


def settings(tmp_path, **over):
    s = config.load()
    base = {"data_dir": tmp_path, "mode": "observe", "env": "demo", "paper_bankroll_usd": 100.0,
            "kelly_fraction": 0.25, "max_usd_per_trade": 5.0, "min_order_usd": 0.0,
            "skip_categories": frozenset(), "commodities_enabled": False}
    return type(s)(**{**s.__dict__, **base, **over})


def test_kelly_is_zero_without_an_edge_and_grows_with_it():
    assert kelly_share(0.05, 0.10) == 0.0
    assert kelly_share(0.10, 0.10) == 0.0  # the fee eats an even bet
    assert 0 < kelly_share(0.20, 0.10) < kelly_share(0.40, 0.10) < 1.0


def test_a_cheap_long_shot_gets_a_small_ticket_and_a_big_payout(tmp_path):
    s = settings(tmp_path)
    spec = lanes.spec_for(s, "moonshot")
    sz = size_bet(spec, s, p=0.20, ask=0.05, available=100, equity=100, lane_exposure=0)
    assert sz.ok
    assert sz.cost_usd <= 100 * spec.max_bet_pct + 1e-9  # one percent of the balance at most
    assert sz.contracts * (1 - 0.05) > 15  # a few dollars staked, a double-digit payout if it hits


def test_a_near_certain_favorite_is_sized_by_its_own_cap_not_by_kelly(tmp_path):
    s = settings(tmp_path)
    spec = lanes.spec_for(s, "steady")
    sz = size_bet(spec, s, p=0.99, ask=0.95, available=100, equity=100, lane_exposure=0)
    assert sz.cost_usd <= 100 * spec.max_bet_pct + 1e-9
    assert sz.cost_usd > 100 * lanes.spec_for(s, "moonshot").max_bet_pct  # larger than a moonshot ticket


def test_stakes_follow_the_balance(tmp_path):
    s = settings(tmp_path)
    spec = lanes.spec_for(s, "steady")
    small = size_bet(spec, s, 0.9, 0.7, available=50, equity=50, lane_exposure=0)
    big = size_bet(spec, s, 0.9, 0.7, available=500, equity=500, lane_exposure=0)
    assert big.contracts >= small.contracts * 3  # and it never exceeds the hard ceiling
    assert big.cost_usd <= s.max_usd_per_trade + 1e-9


def test_a_full_lane_stops_betting_and_a_broke_balance_stops_everything(tmp_path):
    s = settings(tmp_path)
    spec = lanes.spec_for(s, "moonshot")
    full = size_bet(spec, s, 0.3, 0.1, available=100, equity=100, lane_exposure=100 * spec.max_exposure_pct)
    assert not full.ok and "no room" in full.why
    broke = size_bet(spec, s, 0.3, 0.1, available=0.0, equity=100, lane_exposure=0)
    assert not broke.ok
    dust = size_bet(spec, s, 0.3, 0.1, available=100, equity=5, lane_exposure=0)  # 1% of $5 is 5 cents
    assert not dust.ok and "one contract" in dust.why


def shot(ticker="KXA-1", lane="moonshot", ask=0.05, side="yes"):
    close = (datetime.now(UTC) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return LongShot(ticker, "t", "", ask - 0.01, ask, 500.0, close, 10.0, lane=lane, side=side,
                    event_ticker="EV")


class Est:
    def __init__(self, p):
        self.p = p

    def estimate(self, *a, **k):
        return self.p, "because"


def test_paper_bets_are_sized_stored_and_only_staked_once(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    first = runner.evaluate(s, None, Est(0.25), store, shot())
    assert first.action == "bet"
    rows = store.paper_bets(open_only=True)
    assert len(rows) == 1 and rows[0]["stake_usd"] <= 1.0 + 1e-9
    again = runner.evaluate(s, None, Est(0.25), store, shot())
    assert again.action == "bet"  # still our view
    assert len(store.paper_bets(open_only=True)) == 1  # but the second look did not stake again
    assert store.bankroll.read(None, s).available == pytest.approx(100 - rows[0]["stake_usd"])


def test_the_paper_balance_compounds_from_results(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    runner.evaluate(s, None, Est(0.30), store, shot("KXW-1", ask=0.10))
    runner.evaluate(s, None, Est(0.30), store, shot("KXL-1", ask=0.10))
    staked = sum(r["stake_usd"] for r in store.paper_bets(open_only=True))
    assert store.bankroll.read(None, s).equity == pytest.approx(100.0)  # nothing realized yet
    store.set_outcome("KXW-1", "yes")  # the long shot lands
    store.set_outcome("KXL-1", "no")
    bal = bankroll.paper_balance(store, s)
    win = next(r for r in store.paper_bets(open_only=False) if r["ticker"] == "KXW-1")
    assert bal.equity == pytest.approx(100.0 + win["contracts"] - staked)
    assert bal.equity > 100.0  # one 10-cent win pays for several losses
    pnl = bankroll.paper_pnl(store)["moonshot"]
    assert (pnl["n"], pnl["wins"]) == (2, 1)
    assert store.bankroll.read(None, s).open_by_lane == {}


def test_a_no_bet_is_settled_against_its_own_side(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    runner.evaluate(s, None, Est(0.01), store, shot("KXN-1", ask=0.06, side="no"))  # model: YES 1%
    store.set_outcome("KXN-1", "no")
    assert bankroll.paper_pnl(store)["moonshot"]["wins"] == 1


def test_a_bet_too_small_for_the_balance_is_not_taken(tmp_path):
    s = settings(tmp_path, paper_bankroll_usd=3.0)
    store = Store(s.db_path)
    out = runner.evaluate(s, None, Est(0.25), store, shot())
    assert out.action == "pass" and "not sized" in out.note
    assert store.paper_bets(open_only=True) == []


def test_trade_mode_without_credentials_uses_the_paper_balance(tmp_path):
    s = settings(tmp_path, mode="trade")

    class NoKeys:
        authed = False

    assert Store(s.db_path).bankroll.read(NoKeys(), s).source == "paper"


def test_trade_mode_with_credentials_uses_the_kalshi_balance(tmp_path):
    s = settings(tmp_path, mode="trade")

    class Keys:
        authed = True

        def balance(self):
            return {"balance": 4250, "balance_dollars": "42.5000", "portfolio_value": 750}

    bal = Store(s.db_path).bankroll.read(Keys(), s)
    assert (bal.source, bal.available, bal.equity) == ("kalshi", 42.5, 50.0)


# ---- a fixed-stake, always-bet lane (the 15-minute long shots) ----
def example_lane(tmp_path):
    import shutil
    from pathlib import Path

    shutil.copy(Path(__file__).parent.parent / "strategies.example.toml", tmp_path / "strategies.toml")
    return settings(tmp_path)


def test_the_example_lane_loads_and_is_tiered(tmp_path):
    s = example_lane(tmp_path)
    lane = lanes.spec_for(s, "fifteen")
    assert lane.always and lane.fixed_stake == 2.0 and lane.sides == ("yes", "no")
    assert lane.price == (0.12, 0.30) and lane.entry_elapsed == (1.0, 12.0)
    assert lanes.tiered(lane) and "fifteen" in [x.name for x in lanes.tiered_lanes(s)]


def test_always_bets_even_when_the_model_sees_no_edge(tmp_path):
    lane = lanes.spec_for(example_lane(tmp_path), "fifteen")
    assert lanes.wants_bet(lane, p=0.05, ask=0.20, edge=-0.15)  # no edge needed


def test_a_fixed_stake_is_the_same_dollars_whatever_the_odds(tmp_path):
    s = example_lane(tmp_path)
    lane = lanes.spec_for(s, "fifteen")
    cheap = size_bet(lane, s, p=0.05, ask=0.13, available=100, equity=100, lane_exposure=0)
    dear = size_bet(lane, s, p=0.05, ask=0.28, available=100, equity=100, lane_exposure=0)
    assert cheap.ok and dear.ok
    assert 1.7 <= cheap.cost_usd <= 2.0 and 1.7 <= dear.cost_usd <= 2.0  # about $2 either way (whole contracts)
    assert cheap.contracts > dear.contracts  # a cheaper contract buys more of them, and pays more
    assert "fixed stake" in cheap.why


def test_a_fixed_stake_still_respects_cash_and_the_lanes_room(tmp_path):
    s = example_lane(tmp_path)
    lane = lanes.spec_for(s, "fifteen")
    assert size_bet(lane, s, 0.05, 0.2, available=1.0, equity=100, lane_exposure=0).cost_usd <= 1.0
    full = size_bet(lane, s, 0.05, 0.2, available=100, equity=100, lane_exposure=100 * lane.max_exposure_pct)
    assert not full.ok and "no room" in full.why


def test_the_lane_never_trades_for_real_unless_both_switches_are_on(tmp_path):
    s = settings(tmp_path, mode="trade", live_lanes=frozenset({"fifteen"}), fast_live=False)
    assert not s.is_live("fifteen", "KXBTC15M")  # 15-minute windows are paper by default
    on = settings(tmp_path, mode="trade", live_lanes=frozenset({"fifteen"}), fast_live=True)
    assert on.is_live("fifteen", "KXBTC15M")
    assert not on.is_live("commodity", "KXBTC15M")  # naming one lane does not turn on another


def test_paper_sizing_does_not_shrink_with_paper_losses(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    for i in range(3):  # three losing long shots
        runner.evaluate(s, None, Est(0.30), store, shot(f"KXL-{i}", ask=0.10))
        store.set_outcome(f"KXL-{i}", "no")
    assert bankroll.paper_balance(store, s).equity < 100.0  # the record shows the loss
    bal = store.bankroll.read(None, s)
    assert bal.equity == pytest.approx(100.0) and bal.available == pytest.approx(100.0)  # but sizing is not starved
