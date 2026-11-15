"""The numeric loader is one class for the whole process (2026-11-15: it was rebuilt on every Postgres connection and the recorder leaked a class per connection)."""
import gc

import pytest

pytest.importorskip("psycopg")

from kalshi_moonshot import db


def test_unit_the_loader_is_built_once_and_reads_numeric_as_a_float():
    first = db.float_numeric_loader()
    assert db.float_numeric_loader() is first
    assert first(0).load(b"1.50") == 1.5
    assert isinstance(first(0).load(b"12"), float)


def test_integration_repeated_calls_create_no_new_classes():
    db.float_numeric_loader()
    gc.collect()
    before = len([o for o in gc.get_objects() if isinstance(o, type) and o.__name__ == "FloatNumeric"])
    for _ in range(50):
        db.float_numeric_loader()
    gc.collect()
    assert len([o for o in gc.get_objects() if isinstance(o, type) and o.__name__ == "FloatNumeric"]) == before == 1
