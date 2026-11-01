import dataclasses
import json

import pytest

from kalshi_moonshot import config, lanes, settings_sync

TOML = """[lane.commodity]
label = "c"
min_edge = 0.10  # the floor
enabled = true

[lane.moonshot]
max_open = 3
"""


@pytest.fixture
def s(tmp_path):
    return dataclasses.replace(config.load(), data_dir=tmp_path, site_bucket="b")


def test_set_in_toml_keeps_the_comment_and_adds_a_missing_field():
    out = settings_sync.set_in_toml(TOML, "commodity", "min_edge", 0.2)
    assert "min_edge = 0.2  # the floor" in out
    out = settings_sync.set_in_toml(out, "commodity", "enabled", False)
    assert "enabled = false" in out
    out = settings_sync.set_in_toml(out, "moonshot", "min_edge", 0.15)
    assert out.index("min_edge = 0.15") > out.index("[lane.moonshot]")
    assert "min_edge = 0.10" not in out
    lanes.lane_specs_from(config.load(), out)  # still a loadable file


def test_a_lane_that_is_not_in_the_file_is_refused():
    with pytest.raises(ValueError):
        settings_sync.set_in_toml(TOML, "nope", "min_edge", 0.2)


def test_differences_lists_only_values_that_differ_from_the_file(s):
    doc = {"revision": 3, "lanes": {"commodity": {"min_edge": 0.2, "enabled": True}}}
    assert settings_sync.differences(s, TOML, doc) == [("commodity", "min_edge", 0.10, 0.2)]
    assert settings_sync.differences(s, TOML, None) == []


def test_clearing_writes_a_new_revision_with_no_changes_and_an_audit_record():
    class C:
        def __init__(self):
            self.puts = {}

        def put_object(self, Bucket, Key, Body, **kw):
            self.puts[Key] = json.loads(Body)

    c = C()
    rev = settings_sync._clear(c, "b", {"revision": 4, "history": [{"revision": 4}]}, "folded", {"commodity.min_edge": {"from": 0.1, "to": 0.2}})
    assert rev == 5 and c.puts["control/overrides.json"]["lanes"] == {} and c.puts["control/audit/000005.json"]["by"] == "sync"
    assert len(c.puts["control/overrides.json"]["history"]) == 2
