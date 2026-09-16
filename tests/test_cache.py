"""
tests/test_cache.py - Sanity checks for the SQLite cache layer.

Run with: python -m pytest tests/test_cache.py -v
"""
import os
import sys
import tempfile

# Make the project root importable when running pytest from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cache import Cache, temporary_cache


def _tmp_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_filing_roundtrip():
    with temporary_cache(_tmp_path()) as c:
        assert c.get_filing("0000320193", "0000320193-24-000001") is None
        c.put_filing("0000320193", "0000320193-24-000001",
                     "10-Q", "2024-08-01", "aapl-20240629.htm",
                     "Some filing text.")
        row = c.get_filing("0000320193", "0000320193-24-000001")
        assert row is not None
        assert row["content"] == "Some filing text."
        assert row["form"] == "10-Q"


def test_analysis_hit_and_miss():
    with temporary_cache(_tmp_path()) as c:
        content = "Revenue was up. Guidance strong."
        assert c.get_analysis(content, "v1", "llama-3.3-70b-versatile") is None

        c.put_analysis(content, "v1", "llama-3.3-70b-versatile",
                       {"sentiment": "positive", "sentiment_score": 0.8})

        hit = c.get_analysis(content, "v1", "llama-3.3-70b-versatile")
        assert hit == {"sentiment": "positive", "sentiment_score": 0.8}


def test_prompt_version_invalidates_cache():
    """Bumping prompt_version must produce a miss even for identical content."""
    with temporary_cache(_tmp_path()) as c:
        content = "Same content, different prompt."
        c.put_analysis(content, "v1", "m", {"sentiment": "neutral"})

        # v2 has never been stored → must miss.
        assert c.get_analysis(content, "v2", "m") is None
        # Same key still hits.
        assert c.get_analysis(content, "v1", "m") == {"sentiment": "neutral"}


def test_model_invalidates_cache():
    with temporary_cache(_tmp_path()) as c:
        content = "Same content, different model."
        c.put_analysis(content, "v1", "llama", {"sentiment": "positive"})
        assert c.get_analysis(content, "v1", "gpt-4") is None


def test_content_hash_stability():
    """Whitespace changes should change the cache key (they change the prompt)."""
    with temporary_cache(_tmp_path()) as c:
        c.put_analysis("hello", "v1", "m", {"sentiment": "neutral"})
        assert c.get_analysis("hello ", "v1", "m") is None  # trailing space differs
        assert c.get_analysis("hello", "v1", "m") == {"sentiment": "neutral"}


def test_record_and_get_run():
    with temporary_cache(_tmp_path()) as c:
        run_id = c.record_run("AAPL", {"sentiment": "positive", "ticker": "AAPL"})
        assert isinstance(run_id, str) and len(run_id) == 12

        got = c.get_run(run_id)
        assert got is not None
        assert got["ticker"] == "AAPL"
        assert got["result"]["sentiment"] == "positive"
        assert got["created_at"] > 0


def test_get_missing_run():
    with temporary_cache(_tmp_path()) as c:
        assert c.get_run("does-not-exist") is None


def test_put_filing_is_idempotent():
    """Re-inserting the same filing should overwrite, not error."""
    with temporary_cache(_tmp_path()) as c:
        c.put_filing("1", "acc", "8-K", "2024-01-01", "d.htm", "v1")
        c.put_filing("1", "acc", "8-K", "2024-01-01", "d.htm", "v2")
        assert c.get_filing("1", "acc")["content"] == "v2"
