import pytest

from kalshi_moonshot import config, db, paperfill
from kalshi_moonshot.store import Store


def test_a_paper_resting_bid_follows_the_ask_the_way_the_live_lane_does(tmp_path, monkeypatch):
    """buy_rest_follow (2026-11-22, user: "the bot can operate much faster and sees every tick"): the live resting buy moves to `buy_rest_below` under the ask (since 2026-11-24 both ways, a whole tick at a time, at most runner.REST_FOLLOW_MAX times, and re-judged by the lane's module: tests/test_rest_recheck.py), and is withdrawn when the ask falls under the lane's floor. The paper judge does the same on the tape, so a followed paper bet is bought at the lower price the live order would have had, and a lane without the field is judged at its first price as before."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = "estimator='market'\nprice=[0.18,0.40]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=3.0\nseries=['KXBTC15M']\nbuy_rest_seconds=6\nbuy_rest_below=0.02\n"
    (tmp_path / "strategies.toml").write_text(f"[lane.follows]\n{base}buy_rest_follow=true\n\n[lane.stays]\n{base}\n")
    monkeypatch.setenv("LIVE_LANES", "none")
    s = config.load()
    store = Store(s.db_path)
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    tape.execute("CREATE TABLE IF NOT EXISTS flow_1s (ts INTEGER NOT NULL, ticker TEXT NOT NULL, yes_vol REAL NOT NULL, no_vol REAL NOT NULL, n INTEGER NOT NULL, vwap REAL)")

    def quotes(tk, asks):
        for ts, ask in asks:
            tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?, ?, ?, ?)", (ts, tk, round(ask - 0.01, 4), ask))

    quotes("KXBTC15M-FALL", [(99.0, 0.30), (101.0, 0.29), (102.0, 0.28), (103.0, 0.26)])  # decided at 0.30: rests at 0.28, moves to 0.27 (ask 0.29), then 0.26 (ask 0.28), the ask 0.26 reaches it
    quotes("KXBTC15M-FLOOR", [(99.0, 0.20), (101.0, 0.19), (103.0, 0.175)])  # decided at 0.20: rests at 0.18, moves to 0.17 (ask 0.19), and the ask 0.175 is under the lane's 0.18 floor and over the order: withdrawn. The lane that stays at 0.18 is reached by the ask 0.175
    quotes("KXBTC15M-RISE", [(99.0, 0.30), (101.0, 0.33), (103.0, 0.35)])  # a risen ask raises the order (0.31, then 0.33), and the ask never comes down to it: nothing fills
    tape.commit()
    for lane, tk in (("follows", "KXBTC15M-FALL"), ("stays", "KXBTC15M-FALL"), ("follows", "KXBTC15M-FLOOR"), ("stays", "KXBTC15M-FLOOR"), ("follows", "KXBTC15M-RISE")):
        store.add_decision(ticker=tk, title="", bid=0.0, ask=0.20 if "FLOOR" in tk else 0.30, model_p=0.0, edge=0.0, action="bet", mode="observe", lane=lane, side="yes", contracts=3, stake_usd=3.0, close_time="2099-01-01T00:00:00Z", ts=100.0)
    got = paperfill.settle(s, store, tape, now=1000.0)
    rows = {(r["lane"], r["ticker"]): r for r in store.db.execute("SELECT lane, ticker, action, ask, reason FROM decisions").fetchall()}
    fall, stays = rows[("follows", "KXBTC15M-FALL")], rows[("stays", "KXBTC15M-FALL")]
    assert fall["action"] == "bet" and fall["ask"] == pytest.approx(0.26) and "followed the ask from 0.28" in fall["reason"]
    assert stays["action"] == "bet" and stays["ask"] == pytest.approx(0.28) and "followed" not in stays["reason"]  # the same tape, judged at its first price
    assert rows[("follows", "KXBTC15M-FLOOR")]["action"] == "pass"  # withdrawn under the floor
    assert rows[("stays", "KXBTC15M-FLOOR")]["action"] == "bet" and rows[("stays", "KXBTC15M-FLOOR")]["ask"] == pytest.approx(0.18)  # the order that stayed was reached by the drop
    assert rows[("follows", "KXBTC15M-RISE")]["action"] == "pass"
    assert got == {"filled": 3, "missed": 2}
