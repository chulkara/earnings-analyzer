"""
analyzer.py - Fetches multiple SEC EDGAR filings and analyzes them with Groq AI.

Now cache-aware:
  - Raw filing content is cached by (cik, accession) so we hit EDGAR at most once.
  - LLM outputs are cached by (content_hash, prompt_version, model) so we only
    spend tokens when either the content or the prompt itself has changed.

Flow:
  ticker → CIK → 4 recent filings → text extraction (cached)
         → full AI analysis (latest, cached) + quick sentiment (older 3, cached)
         → combined result with history for chart
"""

import html
import json
import re
import requests
from groq import Groq

from cache import get_cache
import transcripts as transcripts_mod


# SEC requires a User-Agent header on every request
EDGAR_HEADERS = {
    "User-Agent": "EarningsAnalyzer contact@example.com",
    "Accept-Encoding": "gzip, deflate",
}


# ─── Model config ────────────────────────────────────────────────────────────
MODEL = "llama-3.3-70b-versatile"


# ─── Prompt constants + versions ─────────────────────────────────────────────
# Bump the version string whenever you change a prompt.
# The cache will treat old outputs as stale automatically.

QUICK_SENTIMENT_PROMPT_VERSION = "v1"

def _quick_sentiment_prompt(content: str, ticker: str, form: str, date: str) -> str:
    excerpt = content[:4000]
    return (
        f'Analyze the financial sentiment of this SEC {form} for {ticker} (filed {date}).\n\n'
        'Return ONLY this JSON (nothing else):\n'
        '{"sentiment": "positive" or "neutral" or "negative", '
        '"sentiment_score": number from -1.0 to 1.0, '
        '"summary": "one sentence about key results"}\n\n'
        f'Filing excerpt:\n{excerpt}'
    )


FULL_ANALYSIS_PROMPT_VERSION = "v2"  # v2: transcript-aware

def _full_analysis_prompt(content: str, ticker: str, company_name: str,
                           form: str, date: str,
                           transcript_excerpt: str = "") -> str:
    # Keep the LLM's context tight even though llama-3.3-70b has 128k
    max_chars = 18000 if not transcript_excerpt else 12000
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n[Document truncated]"

    transcript_section = ""
    if transcript_excerpt:
        transcript_section = (
            "\n\nEARNINGS CALL TRANSCRIPT (same quarter — use for management tone, "
            "analyst pushback, and forward-looking commentary):\n"
            f"{transcript_excerpt}\n"
        )

    return f"""You are a financial analyst. Analyze this SEC {form} filing for {company_name} ({ticker}), filed {date}.

FILING:
{content}
{transcript_section}
Return ONLY a valid JSON object (no text before or after):
{{
  "sentiment": "positive" or "neutral" or "negative",
  "sentiment_score": number from -1.0 to 1.0,
  "summary": "2-3 sentence overview of key results and business developments",
  "key_themes": [
    {{"title": "Theme name", "description": "1-2 sentence explanation", "sentiment": "positive" or "neutral" or "negative"}}
  ],
  "management_tone": "Description of tone and confidence — favor call transcript evidence when available",
  "key_quotes": ["significant direct quote from filing OR transcript (prefix transcript quotes with speaker name)"],
  "guidance": ["forward-looking statement or guidance item"],
  "risks": ["risk factor or concern mentioned"],
  "analyst_concerns": ["question or skepticism from an analyst on the call, if a transcript was provided; otherwise []"],
  "beats_estimates": true or false or null
}}

Include 3-5 key_themes, 2-4 key_quotes, 2-4 guidance items, 2-4 risks, and up to 3 analyst_concerns."""


# ─── Step 1: Ticker → CIK ────────────────────────────────────────────────────

def get_cik_for_ticker(ticker: str) -> tuple:
    """Looks up a company's CIK number using SEC's master company list."""
    response = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=EDGAR_HEADERS, timeout=15
    )
    if response.status_code != 200:
        raise ValueError(f"Could not reach SEC EDGAR (status {response.status_code}).")

    ticker_upper = ticker.upper()
    for entry in response.json().values():
        if entry["ticker"].upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10), entry["title"]

    raise ValueError(
        f"'{ticker}' not found in SEC EDGAR. "
        "Check the ticker is a US-listed company (e.g. AAPL, MSFT, NVDA)."
    )


# ─── Step 2: CIK → Multiple Filing Metadata ──────────────────────────────────

def get_multiple_filing_infos(cik: str, count: int = 4) -> tuple:
    """Returns the last `count` distinct 8-K or 10-Q filings for a company."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    response = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
    if response.status_code != 200:
        raise ValueError(f"Could not fetch filings (status {response.status_code}).")

    data = response.json()
    company_name = data.get("name", "Unknown Company")
    recent = data["filings"]["recent"]

    forms        = recent["form"]
    dates        = recent["filingDate"]
    accessions   = recent["accessionNumber"]
    primary_docs = recent["primaryDocument"]

    filings = []
    seen_months = set()

    for i, form in enumerate(forms):
        if form in ["8-K", "10-Q"]:
            month_key = dates[i][:7]
            if month_key not in seen_months:
                seen_months.add(month_key)
                filings.append({
                    "form":        form,
                    "date":        dates[i],
                    "accession":   accessions[i],
                    "primary_doc": primary_docs[i],
                    "cik":         cik,
                })
                if len(filings) >= count:
                    break

    if not filings:
        raise ValueError("No 8-K or 10-Q filings found for this company.")

    return filings, company_name


# ─── Step 3: Filing → Clean Text (cached) ────────────────────────────────────

def strip_html(raw_html: str) -> str:
    """Converts SEC HTML filing to clean plain text."""
    raw_html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', raw_html,
                      flags=re.DOTALL | re.IGNORECASE)
    raw_html = re.sub(r'<(p|div|br|tr|li|h[1-6])\b[^>]*>', '\n', raw_html,
                      flags=re.IGNORECASE)
    raw_html = re.sub(r'<[^>]+>', '', raw_html)
    raw_html = html.unescape(raw_html)
    raw_html = re.sub(r'[ \t]{2,}', ' ', raw_html)
    raw_html = re.sub(r'\n{3,}', '\n\n', raw_html)
    return raw_html.strip()


def fetch_filing_text(cik: str, accession: str, primary_doc: str,
                      form: str = "", date: str = "") -> str:
    """
    Downloads the primary document of an SEC filing and returns clean text.
    Cached: if we've already fetched this (cik, accession), returns from SQLite.
    """
    cache = get_cache()
    cached = cache.get_filing(cik, accession)
    if cached:
        return cached["content"]

    acc_no_dashes = accession.replace("-", "")
    cik_int = int(cik)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dashes}/{primary_doc}"

    response = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    if response.status_code != 200:
        raise ValueError(f"Could not download filing (status {response.status_code}).")

    raw = response.text
    ct = response.headers.get("Content-Type", "")
    if "html" in ct.lower() or primary_doc.endswith((".htm", ".html")):
        text = strip_html(raw)
    else:
        text = re.sub(r'\n{3,}', '\n\n', raw).strip()

    cache.put_filing(cik, accession, form, date, primary_doc, text)
    return text


# ─── AI Analysis (cached) ────────────────────────────────────────────────────

def _parse_json_response(raw: str) -> dict:
    """Strips markdown fences and parses JSON from a model response."""
    clean = raw.strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.split("\n")[1:-1])
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError("Model returned an unexpected format. Please try again.")


def quick_sentiment_analysis(content: str, ticker: str, form: str, date: str,
                              groq_key: str) -> dict:
    """Fast sentiment for older filings. Cached by (content, prompt_version, model)."""
    cache = get_cache()

    prompt = _quick_sentiment_prompt(content, ticker, form, date)
    cached = cache.get_analysis(prompt, QUICK_SENTIMENT_PROMPT_VERSION, MODEL)
    if cached:
        return {"date": date, "form": form, **cached}

    client = Groq(api_key=groq_key)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=120,
        temperature=0.1,
    )
    result = _parse_json_response(response.choices[0].message.content)

    normalized = {
        "sentiment":       result.get("sentiment", "neutral"),
        "sentiment_score": float(result.get("sentiment_score", 0.0)),
        "summary":         result.get("summary", ""),
    }
    cache.put_analysis(prompt, QUICK_SENTIMENT_PROMPT_VERSION, MODEL, normalized)
    return {"date": date, "form": form, **normalized}


def _try_transcript_for_filing(ticker: str, date: str) -> tuple:
    """
    Best-effort transcript lookup for the filing date.
    Tries the mapped quarter, then the previous one (10-Q filings often
    trail the earnings call by a few weeks).

    Returns (excerpt_str, highlights_list, quarter_str) — all empty if none.
    """
    if not transcripts_mod.is_configured():
        return "", [], ""

    q = transcripts_mod.quarter_for_date(date)
    payload = transcripts_mod.fetch_transcript(ticker, q)
    if not payload:
        prev = transcripts_mod.previous_quarter(q)
        payload = transcripts_mod.fetch_transcript(ticker, prev)
        if payload:
            q = prev

    if not payload:
        return "", [], ""

    excerpt = transcripts_mod.summarize_for_llm(payload)
    highlights = transcripts_mod.key_highlights(payload)
    return excerpt, highlights, q


def analyze_filing(filing: dict, groq_key: str, progress=None) -> dict:
    """Full AI analysis of a single filing. Cached by (content, prompt_version, model)."""
    cache = get_cache()

    ticker       = filing["symbol"]
    company_name = filing.get("company_name", ticker)
    form         = filing["form"]
    date         = filing["date"]
    content      = filing["content"]

    if progress:
        progress("Looking for matching earnings call transcript...")
    transcript_excerpt, transcript_highlights, transcript_quarter = \
        _try_transcript_for_filing(ticker, date)

    prompt = _full_analysis_prompt(
        content, ticker, company_name, form, date, transcript_excerpt
    )
    cached = cache.get_analysis(prompt, FULL_ANALYSIS_PROMPT_VERSION, MODEL)
    if cached:
        analysis = dict(cached)
    else:
        client = Groq(api_key=groq_key)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2200,
            temperature=0.3,
        )
        analysis = _parse_json_response(response.choices[0].message.content)
        cache.put_analysis(prompt, FULL_ANALYSIS_PROMPT_VERSION, MODEL, analysis)

    analysis["ticker"]                = ticker
    analysis["company_name"]          = company_name
    analysis["form"]                  = form
    analysis["date"]                  = date
    analysis["transcript_available"]  = bool(transcript_excerpt)
    analysis["transcript_quarter"]    = transcript_quarter
    analysis["transcript_highlights"] = transcript_highlights
    return analysis


# ─── Main Orchestrator ────────────────────────────────────────────────────────

def analyze_all_filings(ticker: str, groq_key: str, progress=None) -> dict:
    """
    Full pipeline: fetches 4 filings, fully analyzes the latest one,
    and does quick sentiment on the previous 3 for the history chart.

    progress: optional callable(message: str) for live status updates.
    """
    if progress is None:
        progress = lambda msg: None

    ticker = ticker.upper().strip()

    # 1. Resolve ticker → CIK
    progress("Looking up company in SEC EDGAR...")
    cik, company_name = get_cik_for_ticker(ticker)
    progress(f"Found {company_name} — fetching recent filings...")

    # 2. Get metadata for last 4 filings
    filing_infos, _ = get_multiple_filing_infos(cik, count=4)
    total = len(filing_infos)

    # 3. Download text for each filing (cached where possible)
    filings_with_text = []
    for i, info in enumerate(filing_infos):
        progress(f"Loading {info['form']} ({info['date']})  —  {i + 1} of {total}")
        text = fetch_filing_text(
            cik, info["accession"], info["primary_doc"],
            form=info["form"], date=info["date"],
        )
        filings_with_text.append({
            **info,
            "symbol":       ticker,
            "company_name": company_name,
            "content":      text,
        })

    # 4. Full analysis of the most recent filing
    latest = filings_with_text[0]
    progress(f"AI analysis of latest {latest['form']}...")
    full_analysis = analyze_filing(latest, groq_key, progress=progress)

    # 5. Quick sentiment for the older filings
    history = [{
        "date":            full_analysis["date"],
        "form":            full_analysis["form"],
        "sentiment":       full_analysis["sentiment"],
        "sentiment_score": full_analysis.get("sentiment_score", 0.0),
        "summary":         full_analysis.get("summary", ""),
    }]

    older = filings_with_text[1:]
    for i, filing in enumerate(older):
        progress(f"Scoring historical filing {i + 1} of {len(older)}...")
        hist_entry = quick_sentiment_analysis(
            filing["content"], ticker, filing["form"], filing["date"], groq_key
        )
        history.append(hist_entry)

    # Sort oldest → newest for the chart
    history.sort(key=lambda x: x["date"])

    full_analysis["history"]      = history
    full_analysis["company_name"] = company_name

    progress("Done!")
    return full_analysis
