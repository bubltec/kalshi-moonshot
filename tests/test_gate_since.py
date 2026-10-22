import time

from kalshi_moonshot import gate
from kalshi_moonshot.store import Store


def test_since_ts_parses_a_new_york_date_and_ignores_junk():
    assert gate.since_ts("2026-10-22") == 1792641600.0  # midnight EDT
    assert gate.since_ts("") == 0.0 and gate.since_ts("yesterday") == 0.0


def test_bets_before_the_reset_date_do_not_count(tmp_path):
    store = Store(tmp_path / "t.db")
    close = 100_000.0
    for i, opened in enumerate((close - 150, close - 100)):  # both in the 12-15 band
        store.db.execute(
            "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, outcome, close_time, settled_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (opened, f"KXBTC15M-{i}", "bet", "observe", 0.3, "yes", "late_ticket", 3, 1.0, "no",
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(close)), close))
    store.db.commit()
    assert gate.table(store, ("late_ticket",))[(12, 15)].n == 2
    assert gate.table(store, ("late_ticket",), since=close - 125)[(12, 15)].n == 1
