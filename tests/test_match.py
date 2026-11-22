from fieldnote.match import Scored, decide_link, quote_in_source, score_title


def test_exact_title_scores_one():
    assert score_title("CDE folder structure", "CDE folder structure") == 1


def test_weekly_update_is_not_close_enough_to_auto_link():
    assert score_title("weekly update", "Weekly report") < 0.84


def test_client_presentation_is_a_fuzzy_link():
    assert score_title("client presentation", "Client presentation deck") >= 0.84


def test_decide_link_refuses_two_exact_titles():
    scored = [
        Scored("T1", "Weekly report", 1),
        Scored("T2", "Weekly report", 1),
    ]
    decision = decide_link(scored)
    assert decision.task is None
    assert decision.strength == "ambiguous"
    assert len(decision.cousins) == 2


def test_quote_ignores_line_wrapping():
    source = "Mark CDE folder structure\nas blocked"
    assert quote_in_source("Mark CDE folder structure as blocked", source)
    assert not quote_in_source("Mark the whole project as done", source)
