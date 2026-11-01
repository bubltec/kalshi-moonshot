import dataclasses
import json
from dataclasses import fields

import pytest

from kalshi_moonshot import config, control, lanes, overrides


class FakeS3:
    """Just enough of boto3's client: a dict of objects."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            err = Exception("missing")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        body = self.objects[Key]
        return {"Body": type("B", (), {"read": lambda self_: body.encode()})()}

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"].decode()


@pytest.fixture
def s(tmp_path):
    return dataclasses.replace(config.load(), data_dir=tmp_path, site_bucket="b")


def doc(revision=1, **lane):
    return json.dumps({"revision": revision, "lanes": {"commodity": lane or {"min_edge": 0.2}}})


def status(c):
    return json.loads(c.objects["control/status.json"])


def test_every_tunable_is_a_lane_field():
    assert set(overrides.TUNABLES) <= {f.name for f in fields(lanes.LaneSpec)}


@pytest.mark.parametrize("field,value", [("min_edge", 0.9), ("min_edge", -0.1), ("min_edge", "0.1"), ("min_edge", True), ("min_edge", float("nan")), ("max_open", 1.5), ("enabled", 1), ("series", ["KXBTC"]), ("estimator", "x")])
def test_values_outside_the_limits_are_refused(field, value):
    assert overrides.validate({"revision": 1, "lanes": {"commodity": {field: value}}}, {"commodity"})


def test_an_unknown_lane_or_a_malformed_document_is_refused():
    assert overrides.validate({"revision": 1, "lanes": {"nope": {"min_edge": 0.1}}}, {"commodity"})
    assert overrides.validate({"lanes": {}}, {"commodity"})
    assert overrides.validate({"revision": 1, "lanes": []}, {"commodity"})
    assert overrides.validate("x", {"commodity"})
    assert overrides.validate({"revision": 1, "lanes": {"commodity": {"min_edge": 0.1, "max_open": 3, "enabled": False}}}, {"commodity"}) == []


def test_apply_changes_the_lane_and_skips_a_bad_field(s):
    base = lanes.lane_specs(s)
    out = overrides.apply(base, {"revision": 1, "lanes": {"commodity": {"min_edge": 0.2, "max_open": 99}}})
    assert out["commodity"].min_edge == 0.2
    assert out["commodity"].max_open == base["commodity"].max_open  # 99 is over the limit: skipped, the rest still applies
    assert overrides.apply(base, "garbage") is base


def test_a_valid_revision_is_written_and_lane_specs_picks_it_up(s):
    c = FakeS3({"control/overrides.json": doc(3, min_edge=0.21)})
    assert control.sync_once(s, c, now=1_800_000_000.0) is True
    assert json.loads((s.data_dir / "overrides.json").read_text())["revision"] == 3
    assert status(c)["state"] == "applied" and status(c)["revision"] == 3
    assert lanes.lane_specs(s)["commodity"].min_edge == 0.21
    assert control.sync_once(s, c) is False  # the same revision again does nothing


def test_a_refused_revision_is_reported_and_changes_nothing(s):
    before = lanes.lane_specs(s)["commodity"].min_edge
    c = FakeS3({"control/overrides.json": doc(4, min_edge=0.9)})
    assert control.sync_once(s, c) is True
    assert status(c)["state"] == "rejected" and "min_edge" in status(c)["errors"][0]
    assert not (s.data_dir / "overrides.json").exists()
    assert lanes.lane_specs(s)["commodity"].min_edge == before
    assert control.sync_once(s, c) is False  # not retried until the page writes a new revision
    c.objects["control/overrides.json"] = doc(5, min_edge=0.15)
    assert control.sync_once(s, c) is True and status(c)["state"] == "applied"


def test_no_document_means_nothing_to_do(s):
    assert control.sync_once(s, FakeS3()) is False


def test_state_reports_what_the_bot_runs_and_the_pages_history(s):
    c = FakeS3({"control/overrides.json": json.dumps({"revision": 2, "lanes": {}, "history": [{"revision": 2}]})})
    control.sync_once(s, c)
    st = control.state(s, c)
    assert st["applied_revision"] == 2 and st["document"]["history"] == [{"revision": 2}] and st["status"]["state"] == "applied"
