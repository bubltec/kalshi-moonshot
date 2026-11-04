import json

import pytest

from kalshi_moonshot import config, exporter, sitepub
from kalshi_moonshot.store import Store


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


def test_publish_once_uploads_one_checked_object(tmp_path, monkeypatch):
    import dataclasses

    s = dataclasses.replace(config.load(), site_bucket="b")
    store = Store(tmp_path / "m.db", s.exclude_versions)
    s3 = FakeS3()
    size = sitepub.publish_once(s, store, s3, now=1_800_000_000.0)
    put = s3.puts[0]
    assert (put["Bucket"], put["Key"], put["ContentType"]) == ("b", "public/public.json", "application/json")
    assert size == len(put["Body"])
    exporter.validate_public(json.loads(put["Body"]))
    assert "admin" not in put["Key"]


def test_a_file_that_fails_the_allowlist_is_never_uploaded(tmp_path, monkeypatch):
    import dataclasses

    s = dataclasses.replace(config.load(), site_bucket="b")
    store = Store(tmp_path / "m.db", s.exclude_versions)
    monkeypatch.setattr(exporter, "build_public", lambda *a, **k: {"as_of": "x", "min_settled_for_rate": 30, "lanes": [], "pnl_usd": 5})
    s3 = FakeS3()
    with pytest.raises(ValueError):
        sitepub.publish_once(s, store, s3)
    assert s3.puts == []


def test_the_admin_file_goes_to_its_own_private_key(tmp_path):
    import dataclasses

    s = dataclasses.replace(config.load(), site_bucket="b")
    store = Store(tmp_path / "m.db", s.exclude_versions)
    s3 = FakeS3()
    sitepub.publish_admin_once(s, store, s3, now=1_800_000_000.0)
    put = s3.puts[0]
    assert put["Key"] == "admin/admin.json" and put["CacheControl"] == "private, no-store"
    assert not put["Key"].startswith("public/")


def test_a_disabled_lane_is_not_listed_publicly_but_keeps_its_record_in_admin(tmp_path, monkeypatch):
    from kalshi_moonshot import exporter

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_LANES", "child")  # the way the host runs: only the named lanes are on, although the base lane says enabled in the file
    monkeypatch.setenv("PAPER_LANES", "false")
    (tmp_path / "strategies.toml").write_text(
        "[lane.base]\nenabled=true\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXBTC15M']\n"
        "[lane.child]\ninherit='base'\nenabled=true\nseries=['KXETH15M']\n")
    s = config.load()
    names = exporter.public_lane_names(s)
    assert "child" in names and "base" not in names  # the parent a lane inherits from is not itself a public lane
    store = Store(s.db_path)
    for lane in ("base", "child"):
        store.add_decision(ticker=f"KXBTC15M-{lane}", title="", bid=0.0, ask=0.8, model_p=0.0, edge=0.0, action="bet", mode="trade", lane=lane, side="yes", contracts=2, stake_usd=1.6, close_time="2099-01-01T00:00:00Z")
    import time

    later = time.time() + 3 * 86400  # the public page runs a day behind
    public = exporter.public_json(store, later, active=names)
    assert [x["lane"] for x in public["lanes"]] == ["child"]
    admin = exporter.admin_json(store, later, active={"base", "child"})
    assert {x["lane"] for x in admin["lanes"]} == {"base", "child"}  # the record of the switched-off lane stays readable in admin
