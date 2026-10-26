from kalshi_moonshot import config, report, review
from kalshi_moonshot.store import Store


def settings(tmp_path, **over):
    s = config.load()
    base = {
        "data_dir": tmp_path, "steady_enabled": True, "min_edge": 0.05,
        "skip_categories": frozenset(), "rag_skip_lanes": frozenset(),
    }
    return type(s)(**{**s.__dict__, **base, **over})


def row(ticker, lane, p, ask, outcome, action="pass"):
    return {
        "ticker": ticker, "lane": lane, "model_p": p, "ask": ask,
        "outcome": outcome, "action": action, "category": "Sports",
    }


def steady_sample(n=20, p=0.25, ask=0.70, outcome="yes"):
    rows = [row(f"S{i}", "steady", p, ask, outcome) for i in range(n)]
    return [{**r, "category": "unknown"} for r in rows]


def test_a_small_sample_changes_nothing(tmp_path):
    s = settings(tmp_path)
    assert review.propose(steady_sample(10), s) == []


def test_a_weak_steady_scorecard_does_not_turn_the_lane_off(tmp_path):
    s = settings(tmp_path)
    assert review.propose(steady_sample(20), s) == []
    assert config.apply_tuning(s).steady_enabled is True


def test_unknown_and_sports_stay_and_another_category_can_be_skipped(tmp_path):
    s = settings(tmp_path, steady_enabled=False)
    unknown = [{**row(f"U{i}", "moonshot", 0.2, 0.7, "yes"), "category": "unknown"} for i in range(20)]
    sports = [row(f"S{i}", "moonshot", 0.2, 0.7, "yes") for i in range(20)]
    assert review.propose(unknown, s) == []
    assert review.propose(sports, s) == []
    politics = [{**row(f"P{i}", "moonshot", 0.2, 0.7, "yes"), "category": "Politics"} for i in range(20)]
    changes = review.propose(politics, s)
    assert [c.value for c in changes] == ["Politics"]
    review.apply(s, changes)
    assert "Politics" in config.apply_tuning(s).skip_categories


def test_min_edge_rises_only_after_enough_losing_moonshot_bets(tmp_path):
    s = settings(tmp_path, steady_enabled=False)
    few = [row(f"M{i}", "moonshot", 0.4, 0.1, "no", action="bet") for i in range(4)]
    assert review.propose(few, s) == []
    many = [row(f"M{i}", "moonshot", 0.4, 0.1, "no", action="bet") for i in range(10)]
    changes = review.propose(many, s)
    assert changes and changes[0].kind == "raise_edge"
    assert float(changes[0].value) == 0.07


def test_a_skipped_lane_leaves_the_similar_case_index(tmp_path):
    store = Store(tmp_path / "m.db")
    store.add_decision(
        ticker="STEADY1", title="Will Vasteraas happen", ask=0.7, model_p=0.25,
        edge=-0.4, action="pass", mode="observe", lane="steady",
    )
    store.add_decision(
        ticker="MOON1", title="Will Nixon happen", ask=0.05, model_p=0.2,
        edge=0.1, action="pass", mode="observe", lane="moonshot",
    )
    store.set_outcome("STEADY1", "yes")
    store.set_outcome("MOON1", "no")
    assert "Vasteraas" in store.similar_text("Will Vasteraas happen")
    s = settings(tmp_path)
    review.apply(s, [review.Change("rag_skip", "steady", "test")])
    assert store.rebuild_rag() == 1
    assert "Vasteraas" not in store.similar_text("Will Vasteraas happen")
    assert "Nixon" in store.similar_text("Will Nixon happen")


def test_tuning_cannot_turn_steady_back_on_or_lower_the_edge(tmp_path):
    s = settings(tmp_path, steady_enabled=True, min_edge=0.10)
    (tmp_path / "tuning.json").write_text(
        '{"steady_enabled": true, "min_edge": 0.02}\n'
    )
    tuned = config.apply_tuning(s)
    assert tuned.steady_enabled is True
    assert tuned.min_edge == 0.10
    assert report.summarize([])["settled"] == 0


def test_the_hour_review_parses_suggestions_and_ignores_junk():
    got = review._parse_suggestions(
        '{"observations": ["fx stopped 8"], "suggestions": [{"change": "raise FX floor to 0.80", "why": "win 73%", "test": "next hour win%"}, "skip me"]}'
    )
    assert got["observations"] == ["fx stopped 8"]
    assert got["suggestions"] == [{"change": "raise FX floor to 0.80", "why": "win 73%", "test": "next hour win%"}]


def test_hour_suggest_writes_notes_and_never_applies_them(tmp_path, monkeypatch):
    s = settings(tmp_path, review_llm=True, ollama_url="http://127.0.0.1:9", model="x")
    store = Store(tmp_path / "m.db")

    class Fake:
        def ping(self):
            return True

        def complete(self, prompt, tag=""):
            assert "15-MINUTE FAVORITES" in prompt
            return '{"observations": ["crypto won all 74"], "suggestions": [{"change": "do not restore 1.1x", "why": "tp still 97% would have won", "test": "held pnl vs sold"}]}'

    monkeypatch.setattr("kalshi_moonshot.llm.Ollama", lambda url, model: Fake())
    notes = review.hour_suggest(s, store)
    assert notes and "1 suggestion" in notes[0]
    text = (tmp_path / "review-notes.md").read_text()
    assert "do not restore 1.1x" in text and "not applied" in text
    assert not (tmp_path / "tuning.json").exists()  # suggestions never become settings


def test_hour_suggest_stays_quiet_when_the_model_is_down(tmp_path, monkeypatch):
    s = settings(tmp_path, review_llm=True, ollama_url="http://127.0.0.1:9", model="x")

    class Down:
        def ping(self):
            return False

    monkeypatch.setattr("kalshi_moonshot.llm.Ollama", lambda url, model: Down())
    assert review.hour_suggest(s, Store(tmp_path / "m.db")) == ["review llm: ollama not reachable, skipped"]
    assert not (tmp_path / "review-notes.md").exists()
