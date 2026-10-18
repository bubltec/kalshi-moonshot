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


def test_unrelated_question_does_not_match_a_name(tmp_path):
    store = Store(tmp_path / "moonshot.db")
    store.add_decision(
        ticker="KXTEST-2", title="Will Benjamin Netanyahu be arrested before Nov 1, 2026?",
        bid=0.01, ask=0.02, model_p=0.15, edge=0.1, action="bet",
        reason="Arrests of sitting leaders are rare.", mode="observe", close_time="",
    )
    store.set_outcome("KXTEST-2", "no")
    assert store.similar_text("Full Game: Over 84.5 points scored") == ""
