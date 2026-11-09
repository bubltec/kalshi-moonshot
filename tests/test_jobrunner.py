
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


def test_an_overlay_archive_is_unpacked_to_plain_files_only(tmp_path):
    import contextlib
    import io
    import tarfile

    src = tmp_path / "pkg"
    (src / "kalshi_moonshot").mkdir(parents=True)
    (src / "kalshi_moonshot" / "x.py").write_text("v = 1\n")
    archive = tmp_path / "src.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src / "kalshi_moonshot", arcname="kalshi_moonshot")
        evil = tarfile.TarInfo("../escape.txt")
        evil.size = 1
        tar.addfile(evil, io.BytesIO(b"x"))
    dest = tmp_path / "out"
    with contextlib.suppress(Exception):  # the data filter refuses a path outside the folder
        jobrunner.unpack_overlay(archive, dest)
    assert not (tmp_path / "escape.txt").exists()
    clean = tmp_path / "clean.tgz"
    with tarfile.open(clean, "w:gz") as tar:
        tar.add(src / "kalshi_moonshot", arcname="kalshi_moonshot")
    assert (jobrunner.unpack_overlay(clean, tmp_path / "ok") / "kalshi_moonshot" / "x.py").read_text() == "v = 1\n"
