"""Tests must not read the real bot's data directory.

config.load() reads DATA_DIR/tuning.json, where the bot's hourly review writes tightened settings (a higher
minimum edge, skipped categories). Without this, a test's expectations would silently depend on how the live
bot has been doing. Every test gets an empty data directory instead.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "isolated-data"))
