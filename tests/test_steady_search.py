import sqlite3
from datetime import UTC, datetime

from kalshi_moonshot import config, llm, report, risk, search, strategy
from kalshi_moonshot.store import Store

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def settings(**over):
    s = config.load()
    return type(s)(**{**s.__dict__, **over})


def market(**over):
    m = {
        "ticker": "S1", "title": "Will X happen?", "market_type": "binary", "status": "active",
        "yes_bid_dollars": "0.70", "yes_ask_dollars": "0.72", "volume_fp": "900",
        "close_time": "2026-10-19T12:00:00Z", "rules_primary": "rules", "event_ticker": "KXFOO-26",
    }
    m.update(over)
    return m


def candle(ask):
    return {"yes_ask": {"close_dollars": f"{ask:.4f}"}}


# ---- steady lane ----

def test_steady_band_nets_30_to_50_percent_after_fees():
    s = settings()
    assert strategy.steady_reject_reason(market(), s, NOW) is None
    assert 0.30 <= strategy.net_return(s.steady_max_price) <= 0.35
    assert 0.45 <= strategy.net_return(s.steady_min_price) <= 0.50


def test_steady_rejects_outside_band_thin_wide_and_far():
    s = settings()
    assert strategy.steady_reject_reason(market(yes_ask_dollars="0.90", yes_bid_dollars="0.89"), s, NOW)
    assert strategy.steady_reject_reason(market(yes_ask_dollars="0.40", yes_bid_dollars="0.39"), s, NOW)
    assert strategy.steady_reject_reason(market(yes_bid_dollars="0.60"), s, NOW) == "spread too wide"
    assert strategy.steady_reject_reason(market(volume_fp="5"), s, NOW) == "volume too low"
    assert strategy.steady_reject_reason(market(close_time="2026-12-25T12:00:00Z"), s, NOW) == "closes too far out"


def test_steady_skips_sports():
    assert strategy.steady_reject_reason(market(category="Sports"), settings(), NOW) == "category skipped"


def test_trend_requires_a_rise_that_is_not_rolling_over():
    s = settings()
    rising = strategy.ask_trend([candle(a) for a in (0.66, 0.68, 0.70, 0.72)])
    assert rising and strategy.trend_ok(rising, s)
    flat = strategy.ask_trend([candle(a) for a in (0.70, 0.70, 0.71, 0.71)])
    assert flat and not strategy.trend_ok(flat, s)
    rolling = strategy.ask_trend([candle(a) for a in (0.62, 0.70, 0.74, 0.72)])
    assert rolling and not strategy.trend_ok(rolling, s)


def test_trend_needs_enough_history():
    assert strategy.ask_trend([candle(0.7), candle(0.72)]) is None
    assert strategy.ask_trend([{"yes_ask": {}} for _ in range(6)]) is None


# ---- lane cap ----

def test_lane_cap_can_tighten_but_never_loosen(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    s = config.load()
    store = Store(s.db_path)
    assert risk.check(s, store, "A", 1.5, max_trade=2.0).ok
    assert not risk.check(s, store, "A", 3.0, max_trade=2.0).ok
    assert not risk.check(s, store, "A", s.max_usd_per_trade + 1, max_trade=999).ok


# ---- news search ----

FEED = """<rss><channel>
<item><title>Nixon leads new Florida poll &lt;b&gt;by 4&lt;/b&gt; - Tampa Bay Times</title>
<pubDate>Fri, 16 Oct 2026 14:00:00 GMT</pubDate></item>
<item><title>Kalshi odds put Nixon at 9% - Some Blog</title><pubDate>Fri, 16 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Best betting sites for the governor race</title></item>
<item><title>Turnout surges in Florida early voting - AP</title><pubDate>Thu, 15 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_feed_parsing_strips_html_and_drops_odds_headlines():
    lines = search.parse_feed(FEED, limit=5)
    assert len(lines) == 2
    assert "Nixon leads new Florida poll by 4" in lines[0]
    assert "<b>" not in lines[0]
    assert all("odds" not in x.lower() and "betting" not in x.lower() for x in lines)


def test_feed_respects_limit_and_survives_garbage():
    assert len(search.parse_feed(FEED, limit=1)) == 1
    assert search.parse_feed("not xml at all", limit=5) == []


def test_query_leads_with_the_subject():
    q = search.build_query("Who will win the Florida Democratic primary?", "Angie Nixon")
    assert q.startswith("Angie Nixon")
    assert q.endswith("when:14d")


def test_search_failure_returns_empty_and_never_raises(monkeypatch):
    ns = search.NewsSearch()

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ns._http, "get", boom)
    assert ns.context("Will X happen?") == ""


# ---- prompt ----

def test_prompt_includes_context_subject_and_hides_price():
    p = llm.build_prompt(
        "Who will win?", "rules", "2026-11-03", similar="- old case -> no",
        context="- Nixon leads poll", subtitle="Angie Nixon",
    )
    assert "Angie Nixon" in p
    assert "External context for optimal odds" in p
    assert "ignore" in p and "instructions" in p
    assert "- Nixon leads poll" in p and "- old case -> no" in p
    assert "ask" not in p.lower().replace("task", "")
    assert p.rstrip().endswith('"<one sentence>"}')


def test_prompt_without_context_has_no_context_block():
    p = llm.build_prompt("Will X?", "rules", "2026-11-03")
    assert "External context" not in p


# ---- report ----

def drow(ticker, p, ask, outcome, action, lane="moonshot"):
    return {"ticker": ticker, "model_p": p, "ask": ask, "outcome": outcome, "action": action, "lane": lane}


def test_report_counts_each_market_once():
    rows = [drow("A", 0.65, 0.05, "no", "bet")] * 4 + [drow("B", 0.05, 0.05, "no", "pass")]
    s = report.summarize(rows)
    assert s["settled"] == 2
    assert s["bets"] == 1


def test_report_splits_by_lane():
    rows = [
        drow("A", 0.65, 0.05, "no", "bet", "moonshot"),
        drow("B", 0.80, 0.72, "yes", "bet", "steady"),
    ]
    lanes = report.by_lane(rows)
    assert set(lanes) == {"moonshot", "steady"}
    assert lanes["steady"]["bet_wins"] == 1
    assert lanes["moonshot"]["bet_wins"] == 0


def test_report_splits_by_category_and_labels_old_rows_unknown():
    rows = [
        {**drow("A", 0.65, 0.05, "no", "bet"), "category": "Sports"},
        {**drow("B", 0.05, 0.05, "no", "pass"), "category": "Politics"},
        drow("C", 0.10, 0.05, "no", "pass"),
    ]
    cats = report.by_category(rows)
    assert set(cats) == {"Sports", "Politics", "unknown"}
    assert cats["Sports"]["bets"] == 1


def test_prompt_tells_the_model_how_to_treat_totals_and_margins():
    p = llm.build_prompt("Over 84.5 points scored?", "rules", "2026-10-20")
    assert "typical distribution" in p


def test_category_flows_from_market_to_candidate():
    s = settings(skip_categories=frozenset())
    shots = strategy.steady_candidates([market(category="Politics")], s, NOW)
    assert shots and shots[0].category == "Politics"


# ---- migration ----

def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "ticker TEXT NOT NULL, title TEXT, bid REAL, ask REAL, model_p REAL, edge REAL, "
        "action TEXT NOT NULL, reason TEXT, mode TEXT NOT NULL, close_time TEXT, "
        "outcome TEXT, settled_ts REAL)"
    )
    db.execute(
        "INSERT INTO decisions (ts,ticker,action,mode) VALUES (1,'OLD','pass','observe')"
    )
    db.commit()
    db.close()
    store = Store(path)
    row = store.db.execute("SELECT lane, context, category FROM decisions").fetchone()
    assert row["lane"] == "moonshot" and row["context"] is None and row["category"] is None
    store.add_decision(
        ticker="NEW", action="bet", mode="observe", lane="steady", context="x", category="Politics"
    )
    new = store.db.execute("SELECT lane, category FROM decisions WHERE ticker='NEW'").fetchone()
    assert new["lane"] == "steady" and new["category"] == "Politics"


# ---- live-number filter ----
def test_number_threshold_markets_are_skipped_in_both_lanes():
    s = settings(skip_numeric=True)
    cheap = {"yes_ask_dollars": "0.05", "yes_bid_dollars": "0.04"}
    for extra in (
        {"strike_type": "greater", "floor_strike": 81999.99},
        {"strike_type": "less", "cap_strike": 38.4},
        {"strike_type": "between", "floor_strike": 31200, "cap_strike": 31299.99},
        {"strike_type": "greater_or_equal", "floor_strike": 35},
        {"floor_strike": 5.5},
    ):
        assert strategy.reject_reason(market(**cheap, **extra), s, NOW) == strategy.NUMERIC_REASON
        assert strategy.steady_reject_reason(market(**extra), s, NOW) == strategy.NUMERIC_REASON


def test_team_and_date_markets_are_not_numeric():
    s = settings(skip_numeric=True)
    assert not strategy.is_numeric_threshold(market(strike_type="structured"))
    assert not strategy.is_numeric_threshold(market(strike_type="custom", custom_strike={"Date": "Nov 1"}))
    assert not strategy.is_numeric_threshold(market())
    assert strategy.steady_reject_reason(market(strike_type="structured"), s, NOW) is None


def test_numeric_filter_can_be_turned_off():
    s = settings(skip_numeric=False)
    m = market(strike_type="greater", floor_strike=4.3)
    assert strategy.steady_reject_reason(m, s, NOW) is None
