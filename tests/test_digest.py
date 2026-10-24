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
