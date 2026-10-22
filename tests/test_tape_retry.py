from kalshi_moonshot.tape import Tape


class Flaky:
    """A connection whose first write fails, as when the database restarts under the bot."""

    def __init__(self, real):
        self.real, self.fail = real, True

    def executemany(self, *a, **k):
        if self.fail:
            self.fail = False
            raise RuntimeError("terminating connection due to administrator command")
        return self.real.executemany(*a, **k)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_a_failed_flush_keeps_its_rows_for_the_next_one(tmp_path):
    tape = Tape(tmp_path / "t.db")
    tape.db = Flaky(tape.db)
    tape.add({"kind": "quote", "ts": 1.0, "ticker": "KXBTC15M-1", "bid": 0.4, "ask": 0.41})
    tape.add({"kind": "trade", "ts": 1.0, "ticker": "KXBTC15M-1", "taker": "yes", "count": 3.0, "price": 0.41})
    try:
        tape.flush()
        raise AssertionError("the first flush should have failed")
    except RuntimeError:
        pass
    tape.add({"kind": "quote", "ts": 2.0, "ticker": "KXBTC15M-1", "bid": 0.42, "ask": 0.43})
    assert tape.flush() == 3  # both quotes and the second of trade flow, none lost
    assert tape.db.real.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2
    assert tape.db.real.execute("SELECT SUM(yes_vol) FROM flow_1s").fetchone()[0] == 3.0
