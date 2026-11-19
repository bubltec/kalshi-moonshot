"""The tape recorder reads only what it records by from the lane file (lanes.recorder_lanes), so a lane setting its image does not know cannot stop the tape.

2026-11-18: the recorder, on an older image, refused the lane file's new `exit_module` and recorded nothing from 08:16 to 09:00 ET (the same on 2026-11-16 with another field). User: "it should only parse what it needs to record. so the updates to it are less often"."""
import dataclasses


from kalshi_moonshot import config, lanes, runner

BASE = "estimator='market'\nprice=[0.10,0.85]\nhours=[0.0,1.0]\nmax_spread=0.05\nmin_volume=0\n"
GOOD = f"[lane.a]\n{BASE}series=['KXBTC15M','KXETH15M']\nreview_minutes=1\n\n[lane.b]\n{BASE}series=['KXSOL15M']\nreview_minutes=1\n\n[lane.off]\n{BASE}series=['KXOFF15M']\nreview_minutes=1\nenabled=false\n\n[lane.slow]\n{BASE}series=['KXSLOW15M']\n"
NEWER = (GOOD.replace("review_minutes=1\n\n[lane.b]", "review_minutes=1\nsetting_from_a_newer_image=3\nbuy_module='m'\nexit_module='x'\n\n[lane.b]")
         + "\n[module.m]\nsteps=['a_step_this_image_has_not_got']\n\n[exit.x]\n[exit.x.profit]\n[exit.x.loss]\n\n[a_table_from_a_newer_image.y]\nz=1\n")


def settings(tmp_path, monkeypatch, text, role="record", watch="KXWATCH15M"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WATCH_SERIES", watch)
    monkeypatch.delenv("LIVE_LANES", raising=False)
    (tmp_path / "strategies.toml").write_text(text)
    return dataclasses.replace(config.load(), tape_role=role)


def test_unit_only_lanes_that_are_on_and_two_level_are_read_with_their_series(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, GOOD)
    got = dict(lanes.recorder_lanes(s))
    assert got["a"] == ("KXBTC15M", "KXETH15M") and got["b"] == ("KXSOL15M",)
    assert "off" not in got and "slow" not in got


def test_unit_the_series_reach_a_lane_through_inherit_and_through_its_modules_defaults(tmp_path, monkeypatch):
    text = f"[module.m]\nsteps=['price']\nseries=['KXMOD15M']\nreview_minutes=1\n\n[lane.a]\n{BASE}buy_module='m'\n\n[lane.twin]\ninherit='a'\nfixed_stake=2.0\n\n[lane.own]\ninherit='a'\nseries=['KXOWN15M']\n"
    got = dict(lanes.recorder_lanes(settings(tmp_path, monkeypatch, text)))
    assert got["a"] == ("KXMOD15M",) and got["twin"] == ("KXMOD15M",) and got["own"] == ("KXOWN15M",)


def test_unit_the_admin_pages_lane_switch_is_kept(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, GOOD)
    (tmp_path / lanes.overrides.FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / lanes.overrides.FILE).write_text('{"revision": 1, "lanes": {"b": {"enabled": false}}}')
    assert "b" not in dict(lanes.recorder_lanes(s))


def test_unit_on_a_lane_file_both_understand_the_recorder_follows_what_the_bot_follows(tmp_path, monkeypatch):
    rec = runner.watched(settings(tmp_path, monkeypatch, GOOD, role="record"))
    bot = runner.watched(settings(tmp_path, monkeypatch, GOOD, role="both"))
    assert rec == bot and {"KXWATCH15M", "KXBTC15M", "KXETH15M", "KXSOL15M"} <= rec and "KXOFF15M" not in rec and "KXSLOW15M" not in rec  # (the built-in commodity lane names no series, so both add every priced instrument's)


def test_unit_only_live_lanes_add_their_series_when_a_live_list_is_set(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, GOOD)
    s = dataclasses.replace(s, live_lanes=frozenset({"b"}))
    assert runner.watched(s) == {"KXWATCH15M", "KXSOL15M"}

