"""Tests for the diff module — shape, sentiment shift, added/removed sets."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from diff import diff_analyses


def _base(**overrides):
    a = {
        "sentiment": "neutral",
        "sentiment_score": 0.0,
        "key_themes": [],
        "risks": [],
        "analyst_concerns": [],
        "beats_estimates": None,
        "form": "10-Q",
        "date": "2024-01-01",
    }
    a.update(overrides)
    return a


def test_no_material_change_is_flat():
    a = _base(sentiment="positive", sentiment_score=0.3)
    d = diff_analyses(a, a)
    assert d["headline"] == "No material changes"
    assert d["themes_added"] == []
    assert d["risks_added"] == []


def test_sentiment_delta_reported():
    newer = _base(sentiment="positive", sentiment_score=0.6)
    older = _base(sentiment="negative", sentiment_score=-0.4)
    d = diff_analyses(newer, older)
    assert d["sentiment_shift"]["delta"] == 1.0
    assert d["sentiment_shift"]["flipped"] is True
    assert "up" in d["headline"].lower()


def test_beat_flip_detected():
    d = diff_analyses(
        _base(beats_estimates=True),
        _base(beats_estimates=False),
    )
    assert d["beat_flip"] == {"was": False, "now": True}


def test_beat_flip_ignored_when_none():
    d = diff_analyses(_base(beats_estimates=True), _base(beats_estimates=None))
    assert d["beat_flip"] is None


def test_themes_added_and_removed():
    newer = _base(key_themes=[
        {"title": "AI acceleration", "description": "…", "sentiment": "positive"},
        {"title": "Services growth",  "description": "…", "sentiment": "positive"},
    ])
    older = _base(key_themes=[
        {"title": "Services growth",  "description": "…", "sentiment": "positive"},
        {"title": "China exposure",   "description": "…", "sentiment": "negative"},
    ])
    d = diff_analyses(newer, older)
    added   = {t["title"] for t in d["themes_added"]}
    removed = {t["title"] for t in d["themes_removed"]}
    assert added   == {"AI acceleration"}
    assert removed == {"China exposure"}


def test_risks_diff_is_set_based():
    newer = _base(risks=["FX headwinds", "Regulatory scrutiny"])
    older = _base(risks=["FX headwinds", "China exposure"])
    d = diff_analyses(newer, older)
    assert d["risks_added"]   == ["Regulatory scrutiny"]
    assert d["risks_removed"] == ["China exposure"]


def test_new_analyst_concerns():
    newer = _base(analyst_concerns=["Margin trajectory", "Capex ramp"])
    older = _base(analyst_concerns=["Margin trajectory"])
    d = diff_analyses(newer, older)
    assert d["concerns_added"] == ["Capex ramp"]


def test_headline_composes_multiple_signals():
    newer = _base(sentiment="positive", sentiment_score=0.7, beats_estimates=True,
                  key_themes=[{"title": "New", "description": "", "sentiment": "positive"}],
                  risks=["New risk"])
    older = _base(sentiment="neutral", sentiment_score=0.1, beats_estimates=False)
    h = diff_analyses(newer, older)["headline"]
    assert "Sentiment" in h and "beats/misses" in h and "new theme" in h and "new risk" in h
