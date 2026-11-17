"""The always-on loss sale (lane field loss_sale_wobbles; 2026-11-17, user: "We have to have a sale because total losses are devastating"; "it needs to be able to ... correct a bad buy"; "2 wobbles or half")."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import test_risk_exit_flow as F  # noqa: E402

from kalshi_moonshot import config, lanes, riskscore  # noqa: E402
from kalshi_moonshot import ticks as ticks_mod  # noqa: E402
from kalshi_moonshot.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    ticks_mod.mids.clear()
    yield
    ticks_mod.mids.clear()


def test_unit_the_line_is_the_nearer_of_two_wobbles_and_half_the_price():
    assert riskscore.loss_line(0.70, 0.10, 0.72, 2.0) == pytest.approx(0.50)  # two wobbles of 10 cents under the 0.70 bid; half the price (0.36) is further away
    assert riskscore.loss_line(0.18, 0.12, 0.20, 2.0) == pytest.approx(0.10)  # a cheap side cannot fall two wobbles: half the price paid is the line
    assert riskscore.loss_line(0.70, 0.30, 0.72, 2.0) == pytest.approx(0.36)  # a very jumpy market: the wobble line would be 0.10, half the price is nearer
    assert riskscore.loss_line(0.70, None, 0.72, 2.0) == pytest.approx(0.36)  # no wobble reading: the half-price line stands alone, never no line
    assert riskscore.loss_line(0.0, 0.10, 0.72, 2.0) == pytest.approx(0.36)


def test_unit_a_wobble_is_the_typical_one_minute_move_of_the_mid():
    opened = 1000.0
    mids = [(opened + 5.0 * k, 0.60 + (0.02 if k % 2 else 0.0)) for k in range(40)]  # 2 cents every 5 s
    assert riskscore.wobble(mids, opened, opened + 195.0) == pytest.approx(0.02 * 12 ** 0.5, rel=1e-6)
    assert riskscore.wobble(mids[:4], opened, opened + 15.0) is None  # too young to read
    assert riskscore.wobble([(opened + 5.0 * k, 0.60) for k in range(40)], opened, opened + 195.0) is None  # a flat path has no wobble


def test_unit_the_field_makes_a_lane_watched_and_changes_its_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = "[lane.a]\nestimator='ladder'\nprice=[0.1,0.9]\nhours=[0.0,1.0]\n"
    (tmp_path / "strategies.toml").write_text(base)
    s = config.load()
    off = lanes.lane_specs(s)["a"]
    v0 = lanes.version_of(off, s)
    (tmp_path / "strategies.toml").write_text(base + "loss_sale_wobbles=0\n")
    s = config.load()
    assert lanes.version_of(lanes.lane_specs(s)["a"], s) == v0 and not lanes.takes_profit(lanes.lane_specs(s)["a"])
    (tmp_path / "strategies.toml").write_text(base + "loss_sale_wobbles=2\n")
    s = config.load()
    on = lanes.lane_specs(s)["a"]
    assert lanes.version_of(on, s) != v0 and lanes.takes_profit(on)  # a lane whose only exit is the loss sale still gets a watched position


def seed_path(wobble_cents):
    """The market's quotes in memory before the buy: a mid that steps `wobble_cents`/sqrt(12) every 5 s, so one wobble is `wobble_cents`."""
    step = wobble_cents / 100.0 / 12 ** 0.5
    opened = F.CLOSE - 900.0
    for k in range(91):  # 450 s of path up to the buy
        m = 0.80 + (step if k % 2 else 0.0)
        ticks_mod.mids.note(F.TICKER, opened + 5.0 * k, m - 0.005, m + 0.005)


ONLY = """[lane.risk_only]
estimator='ladder'
sides=['yes']
price=[0.70,0.90]
hours=[0.0,0.26]
timed_suffix='15M'
stop_loss=false
{extra}
"""


def lane_only_loss_sale(tmp_path, monkeypatch, client=None, wobbles=2, extra=None):
    """A lane with NO other exit, through the real path (lane file -> refresh -> quote events): anything sold here is sold by the loss sale."""
    from kalshi_moonshot import watcher

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(ONLY.format(extra=f"loss_sale_wobbles={wobbles}" if extra is None else extra))
    s = config.load()
    store = Store(s.db_path)
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (F.CLOSE - 450, F.TICKER, "bet", "trade", 0.81, "yes", "risk_only", 4.65, 3.77, "2030-01-01T00:15:00Z", None))
    store.db.execute("INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd, order_id, side, contract) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (F.CLOSE - 449, F.TICKER, s.env, 4.65, 0.81, 4.65, 3.77, 0.10, "o1", "buy", "yes"))
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, client or F.Client(), lambda: Store(s.db_path))
    w.refresh()
    return w


def test_integration_the_line_is_armed_from_memory_and_the_bet_is_sold_at_it_and_not_before(tmp_path, monkeypatch):
    seed_path(wobble_cents=10)
    w = lane_only_loss_sale(tmp_path, monkeypatch, client=F.Client(bid=0.80))
    t = w.targets[(F.TICKER, "risk_only")]
    assert t.loss_wobbles == 2 and t.loss_line == pytest.approx(0.80 - 0.005 - 0.20, abs=0.02)  # two 10 cent wobbles under the bid at the buy; half the price paid (about 0.39) is further
    assert t.loss_line > riskscore.SALE_SHARE * t.entry
    line = t.loss_line
    for bid in (0.76, 0.70, 0.66, round(line + 0.02, 2)):
        F.yes_bid(w, bid, 440)
        assert F.signals(w) == [], f"sold at {bid}, above the line {line:.2f}"
    F.yes_bid(w, round(line - 0.01, 2), 430)
    out = F.signals(w)
    assert len(out) == 1 and out[0].why.startswith("loss sale: bid ") and "wobbles" in out[0].why


def test_integration_a_cheap_side_is_sold_at_half_the_price_paid(tmp_path, monkeypatch):
    seed_path(wobble_cents=30)  # so jumpy that two wobbles under the bid is below zero
    w = lane_only_loss_sale(tmp_path, monkeypatch, client=F.Client(bid=0.80))
    t = w.targets[(F.TICKER, "risk_only")]
    assert t.loss_line == pytest.approx(riskscore.SALE_SHARE * t.entry) and 0.38 < t.loss_line < 0.41  # half the price paid
    F.yes_bid(w, 0.45, 400)
    assert F.signals(w) == []
    F.yes_bid(w, 0.38, 395)
    out = F.signals(w)
    assert len(out) == 1 and "half the " in out[0].why and " paid" in out[0].why


def test_integration_with_no_path_in_memory_the_bet_still_has_a_line(tmp_path, monkeypatch):
    w = lane_only_loss_sale(tmp_path, monkeypatch, client=F.Client(bid=0.80))
    t = w.targets[(F.TICKER, "risk_only")]
    assert t.loss_line == pytest.approx(riskscore.SALE_SHARE * t.entry) and t.loss_line > 0


def test_integration_the_line_does_not_move_when_the_bet_is_looked_at_again(tmp_path, monkeypatch):
    seed_path(wobble_cents=10)
    w = lane_only_loss_sale(tmp_path, monkeypatch, client=F.Client(bid=0.80))
    line = w.targets[(F.TICKER, "risk_only")].loss_line
    F.yes_bid(w, 0.66, 440)  # the bid has fallen; a later refresh must not re-set the line under it
    w.refresh()
    assert w.targets[(F.TICKER, "risk_only")].loss_line == pytest.approx(line)


def test_integration_the_signal_becomes_a_sell_order_at_a_loss_and_the_bet_closes(tmp_path, monkeypatch):
    seed_path(wobble_cents=10)
    c = F.Client(bid=0.55)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w = lane_only_loss_sale(tmp_path, monkeypatch, client=c)
    w.targets[(F.TICKER, "risk_only")].loss_line = 0.59
    F.yes_bid(w, 0.55, 430)
    job = w._jobs.get_nowait()
    w._sell(*job)
    assert len(c.sold) == 1, "the loss sale raised a signal but no order was placed"
    assert len(Store(config.load().db_path).open_bets()) == 0


def test_integration_off_by_default_nothing_is_armed_or_sold(tmp_path, monkeypatch):
    seed_path(wobble_cents=10)
    w = lane_only_loss_sale(tmp_path, monkeypatch, client=F.Client(bid=0.80), extra="curve_risk_sell=0.05\ncurve_risk_live=true\ncurve_risk_spot_weight=0.0\ncurve_risk_max_loss=0")
    t = w.targets[(F.TICKER, "risk_only")]
    assert t.loss_line == 0.0 and t.loss_wobbles == 0.0
    F.yes_bid(w, 0.30, 430)
    assert not [j for j in F.signals(w) if j.why.startswith("loss sale")]
