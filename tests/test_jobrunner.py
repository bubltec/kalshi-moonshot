
from kalshi_moonshot import jobrunner


def test_the_newest_backup_is_the_last_date_in_the_sorted_names():
    keys = ["backups/tape-20261107.dump", "backups/moonshot-20261108.dump", "backups/tape-20261108.dump", "backups/moonshot-20261107.dump", "backups/pre-pg18-20261024-113303.dump", "backups/tape-20261108.txt"]
    assert jobrunner.newest(keys, "tape-") == "backups/tape-20261108.dump"
    assert jobrunner.newest(keys, "moonshot-") == "backups/moonshot-20261108.dump"
    assert jobrunner.newest(keys, "nothing-") is None


def test_the_job_environment_is_name_value_pairs_and_ignores_anything_else():
    assert jobrunner.parse_job_env("DAYS=5 HOURS=14") == {"DAYS": "5", "HOURS": "14"}
    assert jobrunner.parse_job_env('LABEL="two words" bad-name=1 stray') == {"LABEL": "two words"}
    assert jobrunner.parse_job_env("") == {}


def test_the_files_a_script_leaves_are_the_new_small_ones_and_not_the_inputs(tmp_path):
    import os
    import time

    old = tmp_path / "old.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    start = time.time() - 10
    (tmp_path / "curve_model.json").write_text("{}")
    (tmp_path / "strategies.toml").write_text("x")  # an input
    big = tmp_path / "big.bin"
    big.write_bytes(b"0")
    os.truncate(big, jobrunner.COLLECT_MAX_BYTES + 1)
    (tmp_path / "sub").mkdir()
    assert [p.name for p in jobrunner.new_files(tmp_path, start)] == ["curve_model.json"]
