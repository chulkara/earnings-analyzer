"""
transcripts.py - AlphaVantage earnings call transcripts, cache-first.

AlphaVantage's EARNINGS_CALL_TRANSCRIPT endpoint returns:
  {
    "symbol": "IBM",
    "quarter": "2024Q1",
    "transcript": [
      {"speaker": "...", "title": "...", "content": "...", "sentiment": "0.7"},
      ...
    ]
  }

We cache the whole payload by (symbol, quarter). The API key is read from
the ALPHAVANTAGE_API_KEY env var — it never leaves the server, never touches
the frontend.

If no API key is configured, or AlphaVantage doesn't have the transcript,
we return None cleanly so the rest of the pipeline keeps working.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import requests

from cache import get_cache


ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"
ENV_VAR = "ALPHAVANTAGE_API_KEY"


# ─── Quarter helpers ────────────────────────────────────────────────────────

def quarter_for_date(date_str: str) -> str:
    """
    Map a filing date (YYYY-MM-DD) to the fiscal quarter STRING AlphaVantage
    expects — 'YYYYQN'. A 10-Q filed in August generally reports Q2, filed
    in November → Q3, filed in Feb-May → Q4-of-prior-year or Q1.

    We use the naive rule: quarter = ((month - 1) // 3) + 1 of the
    PREVIOUS calendar quarter, since filings are for the quarter that just
    ended.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    # Filings come out AFTER the quarter closes; back up ~2 months so a
    # 2024-08-01 10-Q (reporting Jun quarter) maps to 2024Q2.
    year, month = dt.year, dt.month - 2
    if month <= 0:
        month += 12
        year -= 1
    q = (month - 1) // 3 + 1
    return f"{year}Q{q}"


def previous_quarter(quarter: str) -> str:
    """'2024Q1' → '2023Q4'."""
    year, q = int(quarter[:4]), int(quarter[-1])
    q -= 1
    if q == 0:
        q = 4
        year -= 1
    return f"{year}Q{q}"


# ─── Fetch ──────────────────────────────────────────────────────────────────

def _api_key() -> Optional[str]:
    key = os.getenv(ENV_VAR, "").strip()
    return key or None


def fetch_transcript(symbol: str, quarter: str) -> Optional[dict]:
    """
    Returns the transcript payload for (symbol, quarter), or None if
    unavailable. Cache-first; only hits AlphaVantage on a miss.
    """
    cache = get_cache()
    cached = cache.get_transcript(symbol, quarter)
    if cached is not None:
        # Empty-list transcript is a valid cached "no data" answer.
        return cached if cached.get("transcript") else None

    key = _api_key()
    if not key:
        return None

    try:
        resp = requests.get(
            ALPHAVANTAGE_URL,
            params={
                "function": "EARNINGS_CALL_TRANSCRIPT",
                "symbol": symbol.upper(),
                "quarter": quarter,
                "apikey": key,
            },
            timeout=20,
        )
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    # AlphaVantage returns errors as {"Information": "..."} or
    # {"Error Message": "..."} with a 200. Treat those as "no data".
    if "Information" in data or "Error Message" in data:
        return None

    if not isinstance(data.get("transcript"), list):
        return None

    # Cache it (even if empty) so we don't re-hit the API for the same miss.
    cache.put_transcript(symbol, quarter, data)
    return data if data["transcript"] else None


# ─── LLM-friendly excerpting ────────────────────────────────────────────────

def summarize_for_llm(payload: dict, max_chars: int = 8000) -> str:
    """
    Compact a full transcript into an excerpt small enough to sit inside the
    analysis prompt without blowing our context budget.

    Strategy: keep speaker + role + content, drop very short filler lines,
    truncate long paragraphs at the sentence boundary, cut off at max_chars.
    """
    entries = payload.get("transcript", []) or []
    lines = []
    used = 0

    for entry in entries:
        speaker = (entry.get("speaker") or "").strip()
        title = (entry.get("title") or "").strip()
        content = (entry.get("content") or "").strip()
        if not content or len(content) < 30:
            continue

        if len(content) > 900:
            # Trim to the last sentence-ish break we can find.
            cut = content[:900]
            last = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
            content = cut[: last + 1] if last > 200 else cut + "…"

        who = f"{speaker} ({title})" if title else speaker
        line = f"{who}: {content}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1

    return "\n\n".join(lines)


def key_highlights(payload: dict, limit: int = 6) -> list[dict]:
    """
    Pull a handful of transcript lines for the frontend — the most opinionated
    (highest |sentiment|) statements from executives and analysts.

    Falls back to the first `limit` substantive lines when sentiment scores
    aren't present.
    """
    entries = payload.get("transcript", []) or []

    def score(e):
        s = e.get("sentiment")
        try:
            return abs(float(s))
        except (TypeError, ValueError):
            return 0.0

    scored = [e for e in entries if len((e.get("content") or "").strip()) >= 60]
    scored.sort(key=score, reverse=True)

    out = []
    for e in scored[:limit]:
        out.append({
            "speaker":   (e.get("speaker") or "").strip(),
            "title":     (e.get("title") or "").strip(),
            "content":   (e.get("content") or "").strip()[:600],
            "sentiment": e.get("sentiment"),
        })
    return out


def is_configured() -> bool:
    """True if the AlphaVantage key is present in the environment."""
    return _api_key() is not None
