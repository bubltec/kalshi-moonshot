from kalshi_moonshot import audit, config
from kalshi_moonshot.store import Store


def tree(tmp_path):
    src = tmp_path / "src" / "kalshi_moonshot"
    src.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (src / "alpha.py").write_text(
        "def used():\n    return helper_in_beta()\n\n\ndef orphan():\n    return 1\n\n\ndef only_tested():\n    return 2\n\n\n"
        "def big():\n" + "".join(f"    value_{i} = compute_something_long({i}) + 1\n" for i in range(30)) + "    return value_0\n"
    )
    (src / "beta.py").write_text(
        "def helper_in_beta():\n    return used()\n\n\ndef twin():\n"
        "    first = fetch_the_record(identifier)\n    second = transform_the_record(first)\n    third = validate_the_record(second)\n"
        "    fourth = store_the_record(third)\n    fifth = announce_the_record(fourth)\n    sixth = finish_the_record(fifth)\n    return sixth\n"
    )
    (src / "gamma.py").write_text(
        "def twin_again():\n    first = fetch_the_record(identifier)\n    second = transform_the_record(first)\n    third = validate_the_record(second)\n"
        "    fourth = store_the_record(third)\n    fifth = announce_the_record(fourth)\n    sixth = finish_the_record(fifth)\n    return sixth\n"
    )
    (tmp_path / "tests" / "test_alpha.py").write_text("from kalshi_moonshot import alpha\n\n\ndef test_it():\n    assert alpha.only_tested() == 2\n")
    return tmp_path


def test_env_names_are_found_through_every_helper_and_example_keys_include_commented_ones(tmp_path):
    src = 'x=_f("ALPHA_ONE", 1)\ny=_b("BETA_TWO", False)\nz=os.environ.get("GAMMA_THREE")\nq=_sides("DELTA_SIDES")\nw=os.environ["EPS_FIVE"]\n'
    assert audit.env_names_in(src) == {"ALPHA_ONE", "BETA_TWO", "GAMMA_THREE", "DELTA_SIDES", "EPS_FIVE"}
    (tmp_path / ".env.example").write_text("# a comment\nKEY_ONE=1\n# KEY_TWO=2\nnot a key\n")
    assert audit.env_file_keys(tmp_path / ".env.example") == {"KEY_ONE", "KEY_TWO"}
    assert audit.env_file_keys(tmp_path / "missing") == set()


def test_dead_code_separates_never_used_from_used_only_by_tests(tmp_path):
    never, tests_only = audit.dead_code(tree(tmp_path))
    assert "alpha.py:5 orphan" in never and "only_tested" not in never and "alpha.py:1 used" not in never
    assert "only_tested" in tests_only and "orphan" not in tests_only


def test_duplicated_blocks_are_reported_with_where_they_are(tmp_path):
    lines = audit.duplication(tree(tmp_path))
    assert any("beta.py" in line and "gamma.py" in line for line in lines)


def test_the_size_report_names_the_longest_function(tmp_path):
    out = "\n".join(audit.size_report(tree(tmp_path)))
    assert "alpha.py" in out and "big" in out


def test_modules_no_test_mentions_are_listed(tmp_path):
    out = audit.test_reach(tree(tmp_path))[0]
    assert "beta" in out and "gamma" in out and "alpha" not in out.split("mentions")[1]


def test_the_rag_check_runs_on_an_empty_index(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(tmp_path / "m.db", frozenset())
    assert "RAG index: 0 cases" in "\n".join(audit.rag_health(store, config.load()))
