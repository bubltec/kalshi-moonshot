"""The wave lane and PriskP do not move each other (2026-11-27; user, on the wave lane buying like PriskP and on a change for one lane moving the other: "make stored readings opt-in per lane, so PriskP's rows are untouched"; "Do not use priskp buy steps").

1. GOLDEN: PriskP's curve reading (wavefacts.curve / leg) calls into waveturn.py, the wave lane's reader. It is replayed on one real market (ZEC 10:30, 2026-11-27) and must equal the golden record, so a change to the wave lane's reader that moves PriskP's reading fails here.
   Regenerate only for a change made to PriskP's own reading on purpose: scripts/priskp_curve_golden.py.
2. The wave lane's reader does not import or name PriskP.
3. Stored step readings are opt-in per lane: a lane that does not name `record_step_facts` keeps its decisions and its decide() exactly as they were."""
import functools
import json
import re
from pathlib import Path

from kalshi_moonshot import buychain, config, lanes, wavetrend, waveturn
from kalshi_moonshot import ticks as ticks_mod
from tests import test_wave_turn as wt
from tests.test_wave_turn import (  # noqa: F401  (mem: the fixture)
    LATE,
    OPEN,
    TICKER,
    K,
    feed,
    fires,
    hour_path,
    mem,
    shot,
    u_of,
    window_path,
)

FIX = Path(__file__).parent / "fixtures"


def test_golden_priskps_curve_reading_on_the_zec_market_is_unchanged(mem):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import priskp_curve_golden as g

    got = g.run()
    want = json.loads((FIX / "priskp_curve_golden_zec.json").read_text())
    assert len(got) == len(want) == 42
    moved = [(a[0], a[1]) for a, b in zip(got, want, strict=True) if a != b]
    assert not moved, f"PriskP's curve reading moved at {moved[:5]}: a change to waveturn.py (the wave lane's reader), wavefacts.py or priskp.py changed what PriskP reads"
    assert sum(1 for a in got if a[2] is not None) == 42  # the market is readable at every look: a golden of Nones would pass for nothing


def test_the_wave_lanes_reader_does_not_name_priskp():
    import inspect

    for mod in (waveturn, wavetrend):
        src = re.sub(r'""".*?"""', "", inspect.getsource(mod), flags=re.DOTALL)
        assert "priskp" not in src.lower(), f"{mod.__name__} names PriskP: the wave lane's reader must not depend on its logic"


def _lane(tmp_path, monkeypatch, opt_in):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    text = wt.TOML if opt_in else wt.TOML.replace("record_step_facts = true\n", "")
    assert ("record_step_facts" in text) is opt_in
    (tmp_path / "strategies.toml").write_text(text)
    return lanes.lane_specs(config.load())["wt"]


def test_a_lane_that_does_not_opt_in_keeps_its_decide_and_its_rows_as_they_were(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import regime, runner
    from kalshi_moonshot.store import Store
    from tests.decide_support import Fixed

    sp = _lane(tmp_path, monkeypatch, opt_in=False)
    assert sp.record_step_facts is False and lanes.LaneSpec.__dataclass_fields__["record_step_facts"].default is False
    s = config.load()
    hour = hour_path()
    window = window_path(LATE, u_of(hour))
    feed(hour, window)
    ticks_mod.strikes.note(TICKER, K)
    t_fire, side = next((t, sd) for t, sd in fires() if t >= 120)
    waveturn.clear()
    feed(hour, window)
    now = OPEN + t_fire + 0.5
    ctx = buychain.Ctx(s, sp, now=now)
    bet, _note = buychain.decide(ctx, shot(now, side))
    assert bet and ctx.facts == {}  # nothing is collected for a lane that did not ask
    waveturn.clear()
    feed(hour, window)
    store = Store(s.db_path)
    regime.reset()
    monkeypatch.setattr(buychain, "Ctx", functools.partial(buychain.Ctx, now=now))
    runner.evaluate(s, None, None, store, shot(now, side), estimators={"market": Fixed()})
    row = store.db.execute("SELECT factors FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert not row["factors"] or "steps" not in json.loads(row["factors"])  # the row is what it was before the readings were stored


def test_a_lane_that_opts_in_collects_and_stores_them(tmp_path, monkeypatch, mem):
    sp = _lane(tmp_path, monkeypatch, opt_in=True)
    assert sp.record_step_facts is True
    s = config.load()
    hour = hour_path()
    feed(hour, window_path([(890, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    ctx = buychain.Ctx(s, sp, now=OPEN + 300.5)
    buychain.decide(ctx, shot(OPEN + 300.5, "yes"))
    assert list(ctx.facts) == ["entry_window", "wave_turn"] and ctx.facts["wave_turn"]["ok"] is False and "swing_u" in ctx.facts["wave_turn"]


def test_the_retired_wave_dip_step_is_gone_from_the_step_list_and_does_not_stop_an_old_lane_file():
    assert "wave_dip" not in lanes.BUY_STEPS and "wave_dip" not in buychain.STEPS and "wave_dip" in lanes.RETIRED_STEPS
