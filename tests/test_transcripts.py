"""
tests/test_transcripts.py - Tests for the AlphaVantage transcript layer.

We don't hit the real API here — instead we monkeypatch requests.get and
verify our cache-first behavior, quarter mapping, and excerpt/highlight
extraction.
"""
import os
import sys
import tempfile
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import cache as cache_mod
import transcripts as transcripts_mod


def _fresh_cache():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = cache_mod.Cache(db_path=path)
    cache_mod._default_cache = c
    return c


# ─── Quarter mapping ────────────────────────────────────────────────────────

def test_quarter_for_date_summer_filing():
    # A 10-Q filed Aug 1 typically reports the Jun-quarter → Q2.
    assert transcripts_mod.quarter_for_date("2024-08-01") == "2024Q2"


def test_quarter_for_date_january_wrap():
    # A filing in early Jan should roll back to prior year Q4.
    assert transcripts_mod.quarter_for_date("2024-01-15") == "2023Q4"


def test_previous_quarter():
    assert transcripts_mod.previous_quarter("2024Q1") == "2023Q4"
    assert transcripts_mod.previous_quarter("2024Q3") == "2024Q2"


# ─── Fetch behavior ─────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
    def json(self):
        return self._payload


def test_no_api_key_returns_none(monkeypatch):
    _fresh_cache()
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    assert transcripts_mod.fetch_transcript("AAPL", "2024Q2") is None


def test_information_error_returns_none(monkeypatch):
    _fresh_cache()
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test")

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({"Information": "demo key"})

    monkeypatch.setattr(transcripts_mod.requests, "get", fake_get)
    assert transcripts_mod.fetch_transcript("AAPL", "2024Q2") is None


def test_successful_fetch_gets_cached(monkeypatch):
    c = _fresh_cache()
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test")

    payload = {
        "symbol": "AAPL",
        "quarter": "2024Q2",
        "transcript": [
            {"speaker": "Tim Cook", "title": "CEO",
             "content": "We had our best quarter ever for iPhone in emerging markets.",
             "sentiment": "0.8"},
        ],
    }

    calls = {"n": 0}
    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(payload)
    monkeypatch.setattr(transcripts_mod.requests, "get", fake_get)

    first = transcripts_mod.fetch_transcript("AAPL", "2024Q2")
    assert first["symbol"] == "AAPL"
    assert len(first["transcript"]) == 1

    # Second call must hit cache, not the network.
    second = transcripts_mod.fetch_transcript("AAPL", "2024Q2")
    assert second["transcript"][0]["speaker"] == "Tim Cook"
    assert calls["n"] == 1


def test_empty_transcript_treated_as_no_data(monkeypatch):
    _fresh_cache()
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test")
    monkeypatch.setattr(transcripts_mod.requests, "get",
                        lambda url, params=None, timeout=None:
                        _FakeResponse({"symbol": "X", "quarter": "2024Q1", "transcript": []}))
    assert transcripts_mod.fetch_transcript("X", "2024Q1") is None


# ─── Summarization / highlights ─────────────────────────────────────────────

def _sample_payload():
    return {
        "symbol": "NVDA",
        "quarter": "2024Q2",
        "transcript": [
            {"speaker": "Operator", "title": "", "content": "hi", "sentiment": "0.0"},
            {"speaker": "Jensen Huang", "title": "CEO",
             "content": "Data center revenue grew 154% year over year — this is the industrial revolution of AI.",
             "sentiment": "0.9"},
            {"speaker": "Analyst A", "title": "Morgan Stanley",
             "content": "Gross margin looks like it peaked. How should we think about it going into next year?",
             "sentiment": "-0.4"},
        ],
    }


def test_summarize_drops_short_lines_and_formats_speakers():
    text = transcripts_mod.summarize_for_llm(_sample_payload())
    assert "Operator" not in text                       # too short, dropped
    assert "Jensen Huang (CEO):" in text
    assert "Analyst A (Morgan Stanley):" in text


def test_summarize_respects_char_budget():
    payload = {"transcript": [
        {"speaker": "S", "title": "", "content": "x" * 2000, "sentiment": "0.5"}
        for _ in range(20)
    ]}
    text = transcripts_mod.summarize_for_llm(payload, max_chars=1000)
    assert len(text) <= 1200  # rough — one line can overrun once


def test_key_highlights_ranks_by_abs_sentiment():
    highs = transcripts_mod.key_highlights(_sample_payload(), limit=2)
    assert len(highs) == 2
    assert highs[0]["speaker"] == "Jensen Huang"        # |0.9| > |-0.4|
    assert highs[1]["speaker"] == "Analyst A"
