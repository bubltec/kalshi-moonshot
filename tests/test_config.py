from kalshi_moonshot import config


def test_apply_env_file_overlays_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("MODE", "observe")
    monkeypatch.setenv("MAX_PRICE", "0.10")
    monkeypatch.delenv("BLANK", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        """
# comment
KALSHI_ENV=prod
MODE="observe"
MAX_PRICE=0.20
BLANK=
"""
    )
    assert config.apply_env_file(path) is True
    s = config.load()
    assert s.env == "prod"
    assert s.mode == "observe"
    assert s.max_price == 0.20
    assert s.data_dir == tmp_path / "data"


def test_apply_env_file_missing_is_a_noop(tmp_path):
    assert config.apply_env_file(tmp_path / "nope") is False
