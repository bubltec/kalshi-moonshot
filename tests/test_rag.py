import pytest

from kalshi_moonshot.store import Store


def test_settled_decision_is_retrievable_without_the_price(tmp_path):
    store = Store(tmp_path / "moonshot.db")
    store.add_decision(
        ticker="KXTEST-1", title="Will Toluca win by more than 3.5 goals?",
        bid=0.01, ask=0.02, model_p=0.12, edge=0.09, action="bet",
        reason="Toluca is the stronger side.", mode="observe", close_time="2026-10-20T01:00:00Z",
    )
    assert store.similar_text("Will Club America win by more than 3.5 goals?") == ""
    store.set_outcome("KXTEST-1", "no")
    text = store.similar_text("Will Club America win by more than 3.5 goals?")
    assert "Toluca" in text
    assert "-> no" in text
    assert "0.02" not in text


def test_a_case_says_when_it_settled(tmp_path):
    store = Store(tmp_path / "moonshot.db")
    store.add_decision(
        ticker="KXTEST-9", title="Will Toluca win by more than 3.5 goals?",
        bid=0.01, ask=0.02, model_p=0.12, edge=0.09, action="bet",
        reason="Toluca is the stronger side.", mode="observe", close_time="2026-10-20T01:00:00Z",
    )
    store.set_outcome("KXTEST-9", "no")
    row = store.db.execute("SELECT settled_ts FROM rag_cases WHERE ticker='KXTEST-9'").fetchone()
    assert row["settled_ts"] > 0  # indexed with its settlement time, so age and recency can be read later
    assert "(settled today)" in store.similar_text("Will Club America win by more than 3.5 goals?")


def test_a_base_rate_prefers_the_last_30_days(tmp_path):
    import time

    store = Store(tmp_path / "moonshot.db")
    for i in range(6):  # two months ago this series resolved yes every time
        _decide(store, f"KXOLD-A{i}", outcome="yes")
    store.db.execute("UPDATE decisions SET settled_ts=? WHERE ticker LIKE 'KXOLD-A%'", (time.time() - 60 * 86400,))
    for i in range(6):  # this month, never
        _decide(store, f"KXOLD-B{i}", outcome="no")
    store.db.commit()
    store.rag.rebuild()
    rate = store.rag.base_rate(series="KXOLD")
    assert rate is not None and (rate.yes, rate.n, rate.recent) == (0, 6, True)
    assert "last 30 days" in rate.line()


def test_an_index_without_settlement_times_is_refilled(tmp_path):
    from kalshi_moonshot.rag import Rag

    store = Store(tmp_path / "moonshot.db")
    _decide(store, "KXBACK-1", outcome="yes")
    store.db.execute("UPDATE rag_cases SET settled_ts=NULL")
    store.db.commit()
    Rag(store)  # opening the index again notices the missing times and backfills from the decisions
    row = store.db.execute("SELECT settled_ts FROM rag_cases WHERE ticker='KXBACK-1'").fetchone()
    assert row["settled_ts"] > 0


def test_unrelated_question_does_not_match_a_name(tmp_path):
    store = Store(tmp_path / "moonshot.db")
    store.add_decision(
        ticker="KXTEST-2", title="Will Benjamin Netanyahu be arrested before Nov 1, 2026?",
        bid=0.01, ask=0.02, model_p=0.15, edge=0.1, action="bet",
        reason="Arrests of sitting leaders are rare.", mode="observe", close_time="",
    )
    store.set_outcome("KXTEST-2", "no")
    assert store.similar_text("Full Game: Over 84.5 points scored") == ""


# ---- strategy versions ----
def _decide(store, ticker, version=None, outcome="no", model_p=0.2, ask=0.1):
    extra = {"version": version} if version else {}
    store.add_decision(ticker=ticker, title=f"Will {ticker} happen", ask=ask, bid=ask, model_p=model_p,
                       edge=0.0, action="pass", mode="observe", **extra)
    store.set_outcome(ticker, outcome)


def test_new_decisions_get_the_current_version(tmp_path):
    from kalshi_moonshot.store import STRATEGY_VERSION
    store = Store(tmp_path / "m.db")
    _decide(store, "AAA")
    assert store.settled_decisions()[0]["version"] == STRATEGY_VERSION


@pytest.mark.sqlite_only
def test_rows_from_before_versions_become_v1(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
               "ticker TEXT NOT NULL, title TEXT, bid REAL, ask REAL, model_p REAL, edge REAL, "
               "action TEXT NOT NULL, reason TEXT, mode TEXT NOT NULL, close_time TEXT, outcome TEXT, settled_ts REAL)")
    db.execute("INSERT INTO decisions (ts,ticker,action,mode) VALUES (1,'OLD','pass','observe')")
    db.commit()
    db.close()
    store = Store(path)
    assert store.version_counts()[0]["version"] == "v1"
    store.add_decision(ticker="NEW", action="pass", mode="observe")
    from kalshi_moonshot.store import STRATEGY_VERSION
    assert {r["version"] for r in store.version_counts()} == {"v1", STRATEGY_VERSION}


def test_excluded_versions_are_left_out_of_settled_and_rag(tmp_path):
    store = Store(tmp_path / "m.db", exclude_versions={"v1"})
    _decide(store, "BADONE", version="v1")
    _decide(store, "GOODONE", version="v2")
    assert [r["ticker"] for r in store.settled_decisions()] == ["GOODONE"]
    assert len(store.settled_decisions(include_excluded=True)) == 2
    assert "BADONE" not in store.similar_text("Will BADONE happen")
    assert "GOODONE" in store.similar_text("Will GOODONE happen")


def test_prune_is_a_dry_run_by_default_and_protects_tickers_with_orders(tmp_path):
    store = Store(tmp_path / "m.db", exclude_versions={"v1"})
    _decide(store, "JUNK", version="v1")
    _decide(store, "HELD", version="v1")
    _decide(store, "KEEP", version="v2")
    store.add_order(ticker="HELD", env="prod", count=1, price=0.1, fill_count=1, cost_usd=0.1, fee_usd=0.01)
    dry = store.prune({"v1"})
    assert dry == {"matched": 2, "kept_for_orders": 1, "deleted": 0}
    assert len(store.settled_decisions(include_excluded=True)) == 3
    done = store.prune({"v1"}, dry_run=False)
    assert done["deleted"] == 1 and (tmp_path / done["backup"].split("/")[-1]).exists()
    left = {r["ticker"] for r in store.settled_decisions(include_excluded=True)}
    assert left == {"HELD", "KEEP"}
