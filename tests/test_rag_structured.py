from kalshi_moonshot.store import Store


def settle(store, ticker, title, outcome, lane="moonshot", category="Politics", version=None):
    extra = {"version": version} if version else {}
    store.add_decision(
        ticker=ticker, title=title, bid=0.05, ask=0.05, model_p=0.2, edge=0.0, action="pass",
        mode="observe", lane=lane, category=category, reason="because", **extra,
    )
    store.set_outcome(ticker, outcome)


def test_same_series_comes_first_and_the_question_itself_is_excluded(tmp_path):
    store = Store(tmp_path / "m.db")
    settle(store, "KXAAA-1", "Will Trump say tariff on Monday", "no")
    settle(store, "KXBBB-1", "Will Biden say tariff on Tuesday", "yes")
    cases = store.rag.cases("Will Trump say tariff", ticker="KXAAA-9", series="KXAAA")
    assert cases[0].title.startswith("Will Trump")
    assert any("Biden" in c.title for c in cases)  # then the wider pool
    again = store.rag.cases("Will Trump say tariff", ticker="KXAAA-1", series="KXAAA")
    assert all("Trump" not in c.title for c in again)  # never retrieves itself


def test_base_rate_uses_the_narrowest_group_with_enough_cases(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(6):
        settle(store, f"KXSER-{i}", f"Will thing {i} happen", "yes" if i < 2 else "no")
    for i in range(10):
        settle(store, f"KXOTH-{i}", f"Will other {i} happen", "no")
    rate = store.rag.base_rate(series="KXSER", category="Politics", lane="moonshot")
    assert (rate.scope, rate.yes, rate.n) == ("series KXSER", 2, 6)
    wide = store.rag.base_rate(series="KXNONE", category="Politics", lane="moonshot")
    assert wide.scope == "category Politics" and wide.n == 16
    assert store.rag.base_rate(series="KXNONE", category="Nothing", lane="nope") is None


def test_block_gives_cases_then_base_rate_and_no_prices(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(6):
        settle(store, f"KXSER-{i}", f"Will tariff thing {i} happen", "no")
    text = store.rag.block("Will tariff news happen", ticker="KXSER-99", category="Politics")
    assert "-> no" in text
    assert "Base rate in our record: 0 of 6" in text
    assert "0.05" not in text


def test_indexing_is_incremental_and_one_row_per_market(tmp_path):
    store = Store(tmp_path / "m.db")
    for _ in range(3):  # re-scored three times
        store.add_decision(
            ticker="KXREP-1", title="Will repeat happen", bid=0.05, ask=0.05, model_p=0.2,
            edge=0.0, action="pass", mode="observe",
        )
    store.set_outcome("KXREP-1", "no")
    count = store.db.execute("SELECT COUNT(*) FROM rag_cases").fetchone()[0]
    assert count == 1
    settle(store, "KXREP-2", "Will another happen", "yes")
    assert store.db.execute("SELECT COUNT(*) FROM rag_cases").fetchone()[0] == 2


def test_commodity_lane_never_enters_the_index(tmp_path):
    store = Store(tmp_path / "m.db")
    settle(store, "KXGOLDD-1", "Gold above 4000", "no", lane="commodity", category="Commodities")
    assert store.db.execute("SELECT COUNT(*) FROM rag_cases").fetchone()[0] == 0


def test_old_index_table_is_dropped_on_open(tmp_path):
    path = tmp_path / "m.db"
    Store(path)
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("CREATE VIRTUAL TABLE rag_fts USING fts5(decision_id UNINDEXED, title, body, outcome)")
    db.commit()
    db.close()
    store = Store(path)
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'rag%'")}
    assert "rag_fts" not in names and "rag_cases" in names


def test_backfill_fills_only_empty_categories_and_backs_up(tmp_path):
    store = Store(tmp_path / "m.db")
    settle(store, "KXOLD-1", "Will old thing happen", "no", category="")
    settle(store, "KXOLD-2", "Will other old thing happen", "yes", category="")
    settle(store, "KXKEEP-1", "Will kept thing happen", "no", category="Politics")
    assert store.uncategorized_series() == {"KXOLD": 2}
    dry = store.backfill_categories({"KXOLD": "Sports", "KXKEEP": "Wrong"})
    assert dry["fillable"] == 2 and dry["updated"] == 0
    assert store.uncategorized_series() == {"KXOLD": 2}  # a dry run changes nothing
    done = store.backfill_categories({"KXOLD": "Sports", "KXKEEP": "Wrong"}, dry_run=False)
    assert done["updated"] == 2 and (tmp_path / done["backup"].split("/")[-1]).exists()
    cats = {r["ticker"]: r["category"] for r in store.db.execute("SELECT ticker, category FROM decisions")}
    assert cats == {"KXOLD-1": "Sports", "KXOLD-2": "Sports", "KXKEEP-1": "Politics"}
    assert store.rag.base_rate(category="Sports") is None  # only 2 cases: below the minimum


def test_backfill_leaves_series_kalshi_cannot_name(tmp_path):
    store = Store(tmp_path / "m.db")
    settle(store, "KXGONE-1", "Will gone happen", "no", category="")
    out = store.backfill_categories({"KXGONE": ""}, dry_run=False)
    assert out["unresolved"] == ["KXGONE"] and out["updated"] == 0
    assert "backup" not in out
