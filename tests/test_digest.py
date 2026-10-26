import time

from kalshi_moonshot import config, digest
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape


def bet(store, ticker, lane, ask, side, outcome, mode="observe", stake=5.0, exit_price=None, reason=None, model_p=0.6, ver="v1"):
    store.add_decision(lane_ver=ver, ticker=ticker, title="t", bid=ask - 0.01, ask=ask, model_p=model_p, edge=0.05, action="bet", mode=mode, lane=lane,
                       side=side, stake_usd=stake, contracts=stake / ask)
    if exit_price is not None:
        store.mark_exited(ticker, exit_price, reason or "peak-crash: bid fell", lane)
    store.set_outcome(ticker, outcome)


def build(tmp_path):
    store = Store(tmp_path / "m.db", frozenset())
    tape = Tape(tmp_path / "t.db")
    bet(store, "A1", "fx_favorite", 0.70, "yes", "yes", mode="trade")                                  # a live win, held
    bet(store, "A2", "fx_favorite", 0.70, "yes", "no", mode="trade", exit_price=0.35, reason="stop-loss: bid 0.35")  # a live stop that would have lost anyway
    bet(store, "B1", "paper_lane", 0.50, "yes", "yes", exit_price=0.58, reason="peak-crash: bid fell")  # sold at 0.58 a bet that paid 1.00
    return store, tape.db


def test_the_lane_table_separates_live_from_paper_and_shows_what_the_exits_cost(tmp_path):
    store, _ = build(tmp_path)
    lines = digest.lane_table(store, time.time() - 3600)
    live = next(line for line in lines if line.startswith("fx_favorite"))
    paper = next(line for line in lines if line.startswith("paper_lane"))
    assert "LIVE" in live and "paper" in paper
    assert paper.split()[2] == "1" and "+" in paper  # one settled bet; sold early, it earned less than holding
    held_gain = 1.0 / (0.5 + digest.fee(0.5)) - 1
    sold_gain = (0.58 - digest.EXIT_SLIP - digest.fee(0.58)) / (0.5 + digest.fee(0.5)) - 1
    assert f"{sold_gain:+.1%}" in paper and f"{held_gain:+.1%}" in paper and f"{sold_gain - held_gain:+.1%}" in paper


def test_a_burst_exit_is_its_own_kind_in_the_table(tmp_path):
    store = Store(tmp_path / "m.db", frozenset())
    bet(store, "X1", "crypto_favorite", 0.88, "yes", "no", exit_price=0.82, reason="burst: 9x the market's normal volume")
    text = "\n".join(digest.exit_table(store, time.time() - 3600))
    assert "burst" in text and "take-profit" not in text


def test_the_hour_context_is_the_15_minute_clock_without_infrastructure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store, _ = build(tmp_path)
    text = digest.hour_context(config.load(), store, hours=1)
    assert "15-MINUTE FAVORITES" in text and "THIS HOUR'S 15-MINUTE BETS" in text and "WHAT THE EXITS DID" in text
    assert "DATA AND INFRASTRUCTURE" not in text and "resting buy" in text


def test_the_exit_table_groups_by_kind_and_compares_sold_with_held(tmp_path):
    store, _ = build(tmp_path)
    text = "\n".join(digest.exit_table(store, time.time() - 3600))
    assert "stop-loss" in text and "peak-crash" in text
    crash = next(line for line in text.splitlines() if line.startswith("peak-crash"))
    assert "-" in crash.split()[-1]  # selling that bet early cost return


def test_an_empty_period_says_so_instead_of_failing(tmp_path):
    store, _ = build(tmp_path)
    assert digest.exit_table(store, time.time() + 3600) == ["no exits in the period"]
    assert digest.lane_table(store, time.time() + 3600)[0].startswith("lane")


def test_the_full_report_has_every_section_and_reads_the_notes_file(tmp_path, monkeypatch):
    store, tape_db = build(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "review-notes.md").write_text("- decide the hand-placed stop\n")
    s = config.load()
    text = "\n".join(digest.report(s, store, tape_db, days=1))
    for head in ("EACH LANE'S BETS", "WHAT THE EXITS DID", "CALIBRATION", "DATA AND INFRASTRUCTURE", "OPEN ITEMS"):
        assert head in text
    assert "decide the hand-placed stop" in text and "archive: off" in text


def test_the_report_without_a_notes_file_says_none_were_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    assert "nothing recorded as open" in "\n".join(digest.notes(config.load()))


def test_a_live_lane_is_split_by_version_so_old_settings_do_not_hide_in_the_new_ones(tmp_path):
    store = Store(tmp_path / "m.db", frozenset())
    bet(store, "C1", "crypto_favorite", 0.80, "yes", "no", mode="trade", ver="aaaa1111")
    bet(store, "C2", "crypto_favorite", 0.80, "yes", "no", mode="trade", ver="aaaa1111")
    bet(store, "C3", "crypto_favorite", 0.45, "yes", "yes", mode="trade", ver="bbbb2222")
    lines = [line for line in digest.lane_table(store, time.time() - 3600) if line.startswith("crypto_favorite")]
    assert len(lines) == 2 and any("aaaa1111" in line and line.split()[3] == "2" for line in lines) and any("bbbb2222" in line for line in lines)


def test_the_queue_report_shows_busy_time_waits_and_expired_live_evaluations(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_LANES", "fx_favorite")
    store = Store(tmp_path / "m.db", frozenset())
    now = time.time()

    def job(kind, status, start, run_at, prio, key=None):
        store.db.execute(
            "INSERT INTO jobs (kind, payload, status, run_at, created_ts, started_ts, finished_ts, attempts, max_attempts, priority, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kind, "{}", status, run_at, run_at, start, None if start is None else start + 2.0, 1, 3, prio, key),
        )

    job("lane_review:fx_favorite", "done", now - 100, now - 110, 20)   # waited 10 s
    job("lane_review:mid_trail", "done", now - 100, now - 190, 10)     # waited 90 s
    job("lane_scope:mid_trail", "done", now - 300, now - 310, 10)
    job("evaluate", "expired", None, now - 50, 20, "x:fx_favorite:KXEURUSD15M-A")
    job("evaluate", "done", now - 40, now - 45, 20, "x:fx_favorite:KXEURUSD15M-B")
    store.db.commit()
    text = "\n".join(digest.queue_health(config.load(), store, now))
    assert "live lanes (priority > 10) 10s" in text and "paper lanes 90s" in text
    assert "fx_favorite: 2 evaluations in 24 h, 1 expired unprocessed" in text and "lane_review" in text.split("\n")[0]
