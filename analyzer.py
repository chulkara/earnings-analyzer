"""
analyzer.py - Fetches multiple SEC EDGAR filings and analyzes them with Groq AI.

Flow:
  ticker → CIK → 4 recent filings → text extraction
         → full AI analysis (latest) + quick sentiment (older 3)
         → combined result with history for chart
"""

import html
import json
import re
import requests
from groq import Groq


# SEC requires a User-Agent header on every request
EDGAR_HEADERS = {
    "User-Agent": "EarningsAnalyzer contact@example.com",
    "Accept-Encoding": "gzip, deflate",
}


# ─── Step 1: Ticker → CIK ────────────────────────────────────────────────────

def get_cik_for_ticker(ticker: str) -> tuple:
    """
    Looks up a company's CIK number using SEC's master company list.
    Returns (cik_padded_10_digits, company_name).
    """
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
    """
    Returns the last `count` distinct 8-K or 10-Q filings for a company.
    Deduplicates by month so we don't pick up amendments of the same filing.
    Returns (list_of_filing_dicts, company_name).
    """
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
            # Use year-month as dedup key (e.g. "2024-11")
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


# ─── Step 3: Filing → Clean Text ─────────────────────────────────────────────

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


def fetch_filing_text(cik: str, accession: str, primary_doc: str) -> str:
    """Downloads the primary document of an SEC filing and returns clean text."""
    acc_no_dashes = accession.replace("-", "")
    cik_int = int(cik)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dashes}/{primary_doc}"

    response = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    if response.status_code != 200:
        raise ValueError(f"Could not download filing (status {response.status_code}).")

    raw = response.text
    ct = response.headers.get("Content-Type", "")
    if "html" in ct.lower() or primary_doc.endswith((".htm", ".html")):
        return strip_html(raw)
    return re.sub(r'\n{3,}', '\n\n', raw).strip()


# ─── AI Analysis ─────────────────────────────────────────────────────────────

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
    """
    Fast, minimal analysis — just sentiment score + one-line summary.
    Used for the 3 older filings in the history chart.
    """
    client = Groq(api_key=groq_key)
    excerpt = content[:4000]

    prompt = (
        f'Analyze the financial sentiment of this SEC {form} for {ticker} (filed {date}).\n\n'
        'Return ONLY this JSON (nothing else):\n'
        '{"sentiment": "positive" or "neutral" or "negative", '
        '"sentiment_score": number from -1.0 to 1.0, '
        '"summary": "one sentence about key results"}\n\n'
        f'Filing excerpt:\n{excerpt}'
    )

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=120,
        temperature=0.1,
    )

    result = _parse_json_response(response.choices[0].message.content)
    return {
        "date":            date,
        "form":            form,
        "sentiment":       result.get("sentiment", "neutral"),
        "sentiment_score": float(result.get("sentiment_score", 0.0)),
        "summary":         result.get("summary", ""),
    }


def analyze_filing(filing: dict, groq_key: str) -> dict:
    """
    Full AI analysis of an SEC filing.
    Used for the most recent filing to populate the whole dashboard.
    """
    client = Groq(api_key=groq_key)

    ticker       = filing["symbol"]
    company_name = filing.get("company_name", ticker)
    form         = filing["form"]
    date         = filing["date"]
    content      = filing["content"]

    # 128k context window on llama-3.3-70b-versatile, but keep prompt tight
    max_chars = 18000
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n[Document truncated]"

    prompt = f"""You are a financial analyst. Analyze this SEC {form} filing for {company_name} ({ticker}), filed {date}.

FILING:
{content}

Return ONLY a valid JSON object (no text before or after):
{{
  "sentiment": "positive" or "neutral" or "negative",
  "sentiment_score": number from -1.0 to 1.0,
  "summary": "2-3 sentence overview of key results and business developments",
  "key_themes": [
    {{"title": "Theme name", "description": "1-2 sentence explanation", "sentiment": "positive" or "neutral" or "negative"}}
  ],
  "management_tone": "Description of tone and confidence level in the filing",
  "key_quotes": ["significant direct quote or statement from the filing"],
  "guidance": ["forward-looking statement or guidance item"],
  "risks": ["risk factor or concern mentioned"],
  "beats_estimates": true or false or null
}}

Include 3-5 key_themes, 2-3 key_quotes, 2-4 guidance items, 2-4 risks."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0.3,
    )

    analysis = _parse_json_response(response.choices[0].message.content)
    analysis["ticker"]       = ticker
    analysis["company_name"] = company_name
    analysis["form"]         = form
    analysis["date"]         = date
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

    # 3. Download text for each filing
    filings_with_text = []
    for i, info in enumerate(filing_infos):
        progress(f"Downloading {info['form']} ({info['date']})  —  {i + 1} of {total}")
        text = fetch_filing_text(cik, info["accession"], info["primary_doc"])
        filings_with_text.append({
            **info,
            "symbol":       ticker,
            "company_name": company_name,
            "content":      text,
        })

    # 4. Full analysis of the most recent filing
    latest = filings_with_text[0]
    progress(f"AI analysis of latest {latest['form']}...")
    full_analysis = analyze_filing(latest, groq_key)

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
