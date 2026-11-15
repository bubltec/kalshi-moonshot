"""The stored sale reason keeps what a profit take writes at its end (2026-11-14): the typical minute and the cushion note were cut off at 300 characters, so a sale could not be explained afterward."""
from kalshi_moonshot.store import Store


def test_a_profit_takes_whole_reason_is_stored_with_its_cushion_note(tmp_path):
    store = Store(tmp_path / "t.db")
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXSOL15M-1", "bet", "trade", 0.60, "yes", "crypto_multiscale", 3, 1.8, "2099-01-01T00:00:00Z"))
    store.db.commit()
    reason = "curve risk exit (profit take): " + "x" * 420 + "; sells at 0.98 for +59.0% of cost [cushion: the spot's chance 0.896 is not over the 0.981 the sale pays] [socket]"
    store.mark_exited("KXSOL15M-1", 0.98, reason, "crypto_multiscale")
    got = store.db.execute("SELECT exit_reason FROM decisions WHERE ticker='KXSOL15M-1'").fetchone()[0]
    assert got.endswith("[socket]") and "[cushion:" in got
    store.db.execute("UPDATE decisions SET exit_ts=NULL, exit_reason=NULL WHERE ticker='KXSOL15M-1'")
    store.db.commit()
    store.mark_exited("KXSOL15M-1", 0.98, "y" * 2000, "crypto_multiscale")
    assert len(store.db.execute("SELECT exit_reason FROM decisions WHERE ticker='KXSOL15M-1'").fetchone()[0]) == 900  # still bounded
