"""Tests must not read the real bot's data directory.

config.load() reads DATA_DIR/tuning.json, where the bot's hourly review writes tightened settings (a higher
minimum edge, skipped categories). Without this, a test's expectations would silently depend on how the live
bot has been doing. Every test gets an empty data directory instead.
"""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: builds or reads a legacy SQLite file; skipped when the suite runs on Postgres")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "isolated-data"))


@pytest.fixture(autouse=True)
def _no_crash_minimum(monkeypatch):
    """The older exit tests use cheap prices and a plain gain to arm; the progress, give-back and spot rules have their own tests."""
    monkeypatch.setenv("CRASH_PROGRESS", "0")
    monkeypatch.setenv("CRASH_GIVEBACK", "0")
    monkeypatch.setenv("CRASH_SPOT_SIGMA", "0")


@pytest.fixture(autouse=True)
def _database(request, tmp_path, monkeypatch):
    """Run the suite on SQLite (the default) or on Postgres: KM_TEST_DATABASE_URL=postgresql://... pytest.

    On Postgres every test gets schemas of its own (the database layer derives them from the test's private paths) and they are dropped
    afterwards, so tests cannot see each other.
    """
    import os

    url = os.environ.get("KM_TEST_DATABASE_URL", "")
    if url and request.node.get_closest_marker("sqlite_only"):
        pytest.skip("legacy SQLite file")
    if not url:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        yield
        return
    monkeypatch.setenv("DATABASE_URL", url)
    from kalshi_moonshot import db, store

    yield
    local = getattr(db.LOCAL, "conns", {})
    for conn in list(local.values()):
        conn.hard_close()
    local.clear()
    store._PREPARED.clear()
    db.drop_test_schemas()
