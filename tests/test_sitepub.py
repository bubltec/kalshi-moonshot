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
