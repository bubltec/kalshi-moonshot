"""Removing dead code must not start a new version of every lane (2026-11-14): the record gate, the nearest-neighbour gate, the learned grid and the stake scaling are gone, and the settings of theirs that every lane's fingerprint carried stay in it as constants. (stop_model joined them on 2026-11-14, when the model stopped being asked about a stop.)"""
from kalshi_moonshot import config, lanes


def spec(**kw):
    return lanes.LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0, **kw)


def test_unit_the_retired_settings_stay_in_the_fingerprint_at_the_values_every_lane_carried(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    got = lanes.settings_of(spec(), config.load())
    assert {k: got[k] for k in lanes._RETIRED_SETTINGS} == lanes._RETIRED_SETTINGS
    assert got["gate_from"] == "()" and got["knn_k"] == "300" and got["grid_z"] == "3.0" and len(lanes._RETIRED_SETTINGS) == 15 and lanes._RETIRED_SETTINGS["stop_model"] == "False"


def test_unit_the_retired_settings_are_no_longer_fields_a_lane_file_may_name(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    names = {f.name for f in lanes.fields(lanes.LaneSpec)}
    assert not names & set(lanes._RETIRED_SETTINGS) and not {"scale", "scale_from", "gate_since"} & names
    try:
        lanes.lane_specs_from(config.load(), "[lane.t]\nestimator='market'\nprice=[0.5,0.9]\nhours=[0.0,1.0]\ngate_from=['t']\n")
    except ValueError as exc:
        assert "gate_from" in str(exc)
    else:
        raise AssertionError("a lane file naming a removed setting must be refused, not silently ignored")
