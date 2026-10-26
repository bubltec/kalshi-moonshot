import time
from datetime import UTC, datetime

import pytest

from kalshi_moonshot import config, esports
from kalshi_moonshot.store import Store

START = esports.start_of("KXCS2GAME-26OCT241400G2AOT-OT")  # 14:00 New York on 24 Oct 2026


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Store(config.load().db_path)


class Client:
    def __init__(self, markets, results=None):
        self.markets, self.results = markets, results or {}

    def series_markets(self, prefix):
        return [m for m in self.markets if m["ticker"].startswith(prefix)]

    def market(self, ticker):
        return {"result": self.results.get(ticker)}


class Model:
    def __init__(self, p=0.7):
        self.p, self.asked = p, []

    def estimate(self, title, rules, close_time, similar="", context="", subtitle=""):
        self.asked.append((title, context, subtitle))
        return self.p, "ranked higher"


class Searcher:
    def context(self, title, subtitle=""):
        return "G2 won their last three"


def market(team, ticker, bid=0.55, ask=0.58):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": f"{team} wins", "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
            "rules_primary": "If it wins", "expected_expiration_time": "2026-10-24T22:00:00Z"}


MARKETS = [market("G2 Ares", "KXCS2GAME-26OCT241400G2AOT-G2A"), market("overTIME", "KXCS2GAME-26OCT241400G2AOT-OT", 0.40, 0.43)]


def test_the_listed_start_is_read_in_new_york_time():
    assert datetime.fromtimestamp(START, UTC).isoformat() == "2026-10-24T18:00:00+00:00"  # EDT is UTC-4
    assert esports.start_of("KXBTC15M") is None


def test_only_a_match_about_to_start_is_forecast_once_with_one_market(store):
    model = Model()
    assert esports.run_once(Client(MARKETS), model, Searcher(), store, now=START - 3 * 3600) == 0  # three hours out: too early
    assert esports.run_once(Client(MARKETS), model, Searcher(), store, now=START + 60) == 0  # already begun: priced from the score, not the news
    assert esports.run_once(Client(MARKETS), model, Searcher(), store, now=START - 3600) == 1
    assert esports.run_once(Client(MARKETS), model, Searcher(), store, now=START - 3000) == 0  # asked once only
    title, context, _subtitle = model.asked[0]
    assert "G2 Ares" in title and "overTIME" in title and "CS2" in title and context == "G2 won their last three"
    row = store.db.execute("SELECT team, rival, p_model, mid FROM esports_forecasts").fetchone()
    assert (row["team"], row["rival"]) == ("G2 Ares", "overTIME") and row["p_model"] == 0.7 and row["mid"] == pytest.approx(0.565)


def test_the_model_never_gets_the_price(store):
    model = Model()
    esports.run_once(Client(MARKETS), model, Searcher(), store, now=START - 3600)
    assert "0.55" not in " ".join(model.asked[0]) and "0.58" not in " ".join(model.asked[0])


def test_a_failed_model_call_saves_nothing_and_is_retried(store):
    class Down(Model):
        def estimate(self, *a, **k):
            raise RuntimeError("busy")

    assert esports.run_once(Client(MARKETS), Down(), Searcher(), store, now=START - 3600) == 0
    assert esports.run_once(Client(MARKETS), Model(), Searcher(), store, now=START - 3000) == 1


def test_results_are_read_only_after_the_match_should_be_over(store):
    esports.run_once(Client(MARKETS), Model(), Searcher(), store, now=START - 3600)
    c = Client(MARKETS, {"KXCS2GAME-26OCT241400G2AOT-G2A": "yes"})
    assert esports.settle(c, store, now=START + 3600) == 0
    assert esports.settle(c, store, now=START + 7 * 3600) == 1
    assert store.db.execute("SELECT outcome FROM esports_forecasts").fetchone()["outcome"] == "yes"


def seed(store, rows):
    base = store.db.execute("SELECT COUNT(*) FROM esports_forecasts").fetchone()[0]
    for i, (p, mid, outcome, spread) in enumerate(rows, start=base):
        store.db.execute(
            "INSERT INTO esports_forecasts (event_ticker, ticker, game, team, rival, ts, start_ts, p_model, bid, ask, mid, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"E{i}", f"E{i}-A", "CS2", "A", "B", time.time(), time.time(), p, mid - spread / 2, mid + spread / 2, mid, outcome))
    store.db.commit()


def test_the_report_waits_for_enough_matches_then_scores_both(store):
    seed(store, [(0.6, 0.6, "yes", 0.02)] * 5)
    assert "too few" in "\n".join(esports.report(store))
    rows = [(0.9, 0.6, "no", 0.02), (0.1, 0.6, "yes", 0.02)] * 6 + [(0.6, 0.6, "yes", 0.02)] * 6 + [(0.9, 0.9, "yes", 0.30)]
    seed(store, rows)  # model overconfident and wrong where it disagrees; the wide-book match is left out of the score
    out = "\n".join(esports.report(store))
    assert "model 0.4" in out and "market 0.2" in out and "not yet" in out and "within 6c" in out


class Rankings:
    def __init__(self, text="G2 Ares: #8"):
        self.text_value, self.asked = text, []

    def text(self, game, team, rival):
        self.asked.append((game, team, rival))
        return self.text_value


class TwoFaced(Model):
    """Answers 0.7 from the news alone and 0.9 once it has been shown rankings."""

    def estimate(self, title, rules, close_time, similar="", context="", subtitle=""):
        self.asked.append((title, context, subtitle))
        return (0.9, "ranked") if "G2 Ares: #8" in context else (0.7, "news")


def test_each_match_gets_a_news_forecast_and_a_ranked_one(store):
    model = TwoFaced()
    r = Rankings()
    assert esports.run_once(Client(MARKETS), model, Searcher(), store, now=START - 3600, rankings=r) == 1
    row = store.db.execute("SELECT p_model, p_rank, ranking, rank_why FROM esports_forecasts").fetchone()
    assert row["p_model"] == 0.7 and row["p_rank"] == 0.9 and row["ranking"] == "G2 Ares: #8" and row["rank_why"] == "ranked"
    assert r.asked == [("CS2", "G2 Ares", "overTIME")] and len(model.asked) == 2
    assert model.asked[1][1].startswith("G2 Ares: #8\n") and "G2 won" in model.asked[1][1]  # rankings first, then the news
    assert "G2 Ares: #8" not in model.asked[0][1]  # the news-only forecast never sees them


def test_no_ranking_text_means_no_ranked_forecast(store):
    model = TwoFaced()
    esports.run_once(Client(MARKETS), model, Searcher(), store, now=START - 3600, rankings=Rankings(""))
    row = store.db.execute("SELECT p_model, p_rank FROM esports_forecasts").fetchone()
    assert row["p_model"] == 0.7 and row["p_rank"] is None and len(model.asked) == 1


def test_the_report_compares_the_two_forecasts_on_the_same_matches(store):
    for i in range(12):
        store.db.execute(
            "INSERT INTO esports_forecasts (event_ticker, ticker, game, team, rival, ts, start_ts, p_model, p_rank, bid, ask, mid, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"R{i}", f"R{i}-A", "CS2", "A", "B", time.time(), time.time(), 0.5, 0.8, 0.59, 0.61, 0.6, "yes" if i % 5 else "no"))
    store.db.commit()
    out = "\n".join(esports.report(store))
    assert "12 matches" in out and "news only" in out and "news plus rankings" in out and "rankings minus news" in out
