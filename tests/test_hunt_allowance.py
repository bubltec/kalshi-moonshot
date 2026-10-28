import dataclasses

from kalshi_moonshot import buyer, config, lanes


def spec(tmp_path, monkeypatch, **over):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    return dataclasses.replace(lanes.spec_for(s, "moonshot"), fixed_stake=1.0, **over)


def test_the_default_allowance_is_the_old_two_cents(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.hunt_allowance == 0.02
    plan = buyer.hunt_plan(sp, "KXBTC15M-1", "yes", 0.15, 2_000_000_000.0, "r", {}, None)
    assert plan.allowance == 0.02 and plan.paper_contracts == buyer.whole_contracts(1.0, 0.15) == 5.0


def test_a_lane_can_let_the_limit_reach_further_and_the_stake_is_sized_with_it(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch, hunt_allowance=0.05)
    plan = buyer.hunt_plan(sp, "KXBTC15M-1", "yes", 0.15, 2_000_000_000.0, "r", {}, None)
    assert plan.allowance == 0.05
    assert plan.paper_contracts == buyer.whole_contracts(1.0, 0.15, 0.05) == 5.0  # $1 / 0.20
    assert buyer.whole_contracts(1.0, 0.15, 0.10) == 4.0  # a bigger allowance sizes smaller: the worst fill is still inside the stake


def test_the_live_order_limit_is_the_ask_plus_the_plans_allowance(tmp_path, monkeypatch):
    from kalshi_moonshot import runner
    from kalshi_moonshot.store import Store

    sp = spec(tmp_path, monkeypatch, hunt_allowance=0.05)
    s = config.load()
    store = Store(s.db_path)
    seen = {}

    class Venue:
        authed = True

    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    monkeypatch.setattr(runner, "place", lambda settings, client, st, shot, why, count, **kw: (seen.update(limit=shot.ask, count=count) or runner.Placed("bought", count, 0.4)))
    plan = buyer.hunt_plan(sp, "KXBTC15M-1", "yes", 0.15, 2_000_000_000.0, "r", {}, Venue())
    buyer.buy(plan, s, store)
    assert abs(seen["limit"] - 0.20) < 1e-9 and seen["count"] == 5.0
