"""
data_sources.py - Free data sources for insider and congressional trading.

Insider trading:  SEC EDGAR Form 4 filings     (no API key needed)
Congressional:    House Clerk PTR PDFs (disclosures-clerk.house.gov)
"""

import io
import re
import time
import zipfile
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from pypdf import PdfReader

EDGAR_HEADERS = {
    "User-Agent": "EarningsAnalyzer contact@example.com",
    "Accept-Encoding": "gzip, deflate",
}

# Human-readable labels for SEC Form 4 transaction codes
TRANSACTION_CODES = {
    "P": "Purchase",
    "S": "Sale",
    "A": "Award",
    "D": "Disposition",
    "F": "Tax Withholding",
    "G": "Gift",
    "M": "Option Exercise",
    "X": "Derivative Exercise",
    "C": "Conversion",
    "J": "Other",
}


# ── Insider Trading ───────────────────────────────────────────────────────────

def _parse_one_form4(cik: str, accession: str, primary_doc: str, filing_date: str) -> list:
    """
    Downloads and parses a single Form 4 XML file.
    Returns a list of transaction dicts (one per buy/sell row).
    Returns [] silently on any error so one bad filing doesn't break the whole batch.
    """
    try:
        acc = accession.replace("-", "")
        # primaryDocument sometimes has an XSLT subdirectory prefix (e.g. "xslF345X05/foo.xml")
        # that serves a styled HTML page — strip it to reach the raw XML.
        doc = primary_doc.split("/")[-1] if "/" in primary_doc else primary_doc
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=6)
        if resp.status_code != 200:
            return []

        root = ET.fromstring(resp.content)

        # Who filed this? (the insider)
        name  = (root.findtext(".//rptOwnerName") or "Unknown").strip()
        title = (root.findtext(".//officerTitle") or "").strip()

        if not title:
            if (root.findtext(".//isDirector") or "0").strip() == "1":
                title = "Director"
            elif (root.findtext(".//isTenPercentOwner") or "0").strip() == "1":
                title = "10% Owner"
            else:
                title = "Insider"

        rows = []

        # Only look at non-derivative (direct stock) transactions
        for txn in root.findall(".//nonDerivativeTransaction"):
            code     = (txn.findtext(".//transactionCode") or "").strip()
            date_val = (txn.findtext(".//transactionDate/value") or filing_date).strip()
            shares_s = (txn.findtext(".//transactionShares/value") or "0").strip()
            price_s  = (txn.findtext(".//transactionPricePerShare/value") or "0").strip()

            try:
                shares = float(shares_s)
                price  = float(price_s)
            except ValueError:
                continue

            if shares == 0:
                continue  # Skip phantom entries

            rows.append({
                "name":    name,
                "title":   title,
                "type":    TRANSACTION_CODES.get(code, code or "—"),
                "code":    code,
                "is_buy":  code in ("P", "A", "M", "X", "C"),
                "is_sell": code in ("S", "D", "F"),
                "shares":  shares,
                "price":   price,
                "value":   shares * price,
                "date":    date_val,
            })

        return rows

    except Exception:
        return []


def get_insider_trades(cik: str, max_filings: int = 12) -> list:
    """
    Fetches the most recent Form 4 filings for a company and returns all
    parsed transactions sorted newest-first.

    Uses a ThreadPoolExecutor (5 workers) to fetch multiple XMLs in parallel
    while staying under SEC's rate limit guidelines.
    """
    url  = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
    if resp.status_code != 200:
        return []

    data   = resp.json()
    recent = data["filings"]["recent"]
    forms  = recent["form"]
    dates  = recent["filingDate"]
    accs   = recent["accessionNumber"]
    docs   = recent["primaryDocument"]

    # Collect Form 4 metadata (cap at max_filings)
    form4s = []
    for i, form in enumerate(forms):
        if form == "4":
            form4s.append({
                "accession":   accs[i],
                "primary_doc": docs[i],
                "date":        dates[i],
            })
            if len(form4s) >= max_filings:
                break

    if not form4s:
        return []

    # Fetch all Form 4 XMLs in parallel
    all_txns = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(
                _parse_one_form4, cik,
                f["accession"], f["primary_doc"], f["date"]
            ): f
            for f in form4s
        }
        for fut in as_completed(futures):
            all_txns.extend(fut.result())

    # Sort newest first and return (cap at 60 rows for the table)
    all_txns.sort(key=lambda x: x["date"], reverse=True)
    return all_txns[:60]


# ── Congressional Trading ─────────────────────────────────────────────────────
#
# Source: House Clerk Financial Disclosure PTR (Periodic Transaction Reports)
#   Index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip
#   PDFs:  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf
#
# Strategy:
#   1. Download the annual FD zip to get a list of PTR filings (fast, cached 24h).
#   2. Scan the most recent MAX_PTRS PDFs in parallel for the requested ticker.
#   3. Cache per-ticker results for 1 hour.

_CACHE_TTL      = 3600        # seconds – per-ticker results cache
_FD_CACHE_TTL   = 86400       # seconds – FD index cache (daily)
_MAX_PTRS       = 30          # how many recent PTRs to scan per query
_PTR_WORKERS    = 12          # parallel PDF downloads (cap for free-tier CPU)

_fd_index_cache: dict = {"ptrs": None, "ts": 0.0}   # list[dict]
_ticker_cache:   dict = {}                            # ticker → (list, timestamp)

# Matches: (<TICKER>) [ASSET_TYPE]  then on the next line(s): P or S + dates + amount
# Dates are concatenated: MM/DD/YYYY  MM/DD/YYYY  (transaction then notification).
# Amount range may wrap across lines, e.g. "$15,001 -\n$50,000".
_TRADE_RE = re.compile(
    r"\(([A-Z]{1,5})\)\s+\[[A-Z]+\]"           # (TICKER) [ST] etc.
    r"\n([PS])(?:\s+\([^)]*\))?"               # newline, then P or S (optional qualifier)
    r"\s+(\d{2}/\d{2}/\d{4})"                  # transaction date
    r"\d{2}/\d{2}/\d{4}"                        # notification date (ignored)
    r"([\$\d,\s\-]+?)(?=\n)",                   # amount range (until next newline)
)


def _fetch_fd_index() -> list:
    """
    Downloads and parses the current-year (and previous-year) House FD zip
    to build a list of PTR filing dicts sorted newest-first.
    Cached for 24 hours.
    """
    global _fd_index_cache
    if (_fd_index_cache["ptrs"] is not None
            and time.time() - _fd_index_cache["ts"] < _FD_CACHE_TTL):
        return _fd_index_cache["ptrs"]

    current_year = datetime.utcnow().year
    ptrs = []

    for year in (current_year, current_year - 1):
        url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                xml_bytes = z.read(f"{year}FD.xml")
            root = ET.fromstring(xml_bytes.decode("utf-8-sig"))
            for m in root.findall("Member"):
                if m.findtext("FilingType") != "P":
                    continue
                first  = (m.findtext("First")  or "").strip()
                last   = (m.findtext("Last")   or "").strip()
                state  = (m.findtext("StateDst") or "").strip()
                filing_date = (m.findtext("FilingDate") or "").strip()
                doc_id = (m.findtext("DocID")  or "").strip()
                year_s = (m.findtext("Year")   or str(year)).strip()
                ptrs.append({
                    "name":  f"{first} {last}".strip(),
                    "state": state,
                    "year":  year_s,
                    "doc_id": doc_id,
                    "filing_date": filing_date,
                })
        except Exception:
            continue

    # Sort newest first by filing date
    def _parse_date(d: str):
        try:
            return datetime.strptime(d, "%m/%d/%Y")
        except Exception:
            return datetime.min

    ptrs.sort(key=lambda x: _parse_date(x["filing_date"]), reverse=True)
    _fd_index_cache["ptrs"] = ptrs
    _fd_index_cache["ts"]   = time.time()
    return ptrs


def _scan_ptr_for_ticker(ptr: dict, ticker: str) -> list:
    """
    Downloads a single PTR PDF and returns trades matching `ticker`.
    Returns [] silently on any error.
    """
    try:
        doc_id = ptr["doc_id"]
        year   = ptr["year"]
        url    = (f"https://disclosures-clerk.house.gov"
                  f"/public_disc/ptr-pdfs/{year}/{doc_id}.pdf")
        resp   = requests.get(url, timeout=8)
        if resp.status_code != 200:
            return []

        reader = PdfReader(io.BytesIO(resp.content))
        text   = "\n".join(page.extract_text() or "" for page in reader.pages)

        # Quick check before full regex scan
        if f"({ticker})" not in text:
            return []

        trades = []
        for m in _TRADE_RE.finditer(text):
            t, txn_type, txn_date, amount = m.groups()
            if t != ticker:
                continue
            trades.append({
                "representative":   ptr["name"],
                "party":            "",   # not in FD XML
                "state":            ptr["state"],
                "type":             "Purchase" if txn_type == "P" else "Sale",
                "transaction_date": txn_date,
                "disclosure_date":  ptr["filing_date"],
                "amount":           amount.strip().replace("\n", " "),
                "ticker":           ticker,
            })
        return trades
    except Exception:
        return []


def get_congressional_trades(ticker: str) -> list:
    """
    Scans the most recent House PTR filings (Periodic Transaction Reports)
    for trades in `ticker` and returns them newest-first.

    Uses a two-level cache:
      - FD zip index: cached 24 h (list of all PTR filings).
      - Per-ticker results: cached 1 h.
    """
    global _ticker_cache
    ticker = ticker.upper().strip()

    # Return cached per-ticker results if fresh
    if ticker in _ticker_cache:
        cached_trades, cached_ts = _ticker_cache[ticker]
        if time.time() - cached_ts < _CACHE_TTL:
            return cached_trades

    ptrs = _fetch_fd_index()
    if not ptrs:
        return []

    # Scan the most recent MAX_PTRS PTRs in parallel
    all_trades: list = []
    subset = ptrs[:_MAX_PTRS]
    with ThreadPoolExecutor(max_workers=_PTR_WORKERS) as pool:
        futures = {pool.submit(_scan_ptr_for_ticker, ptr, ticker): ptr
                   for ptr in subset}
        for fut in as_completed(futures):
            all_trades.extend(fut.result())

    # Sort newest transaction first
    def _sort_key(t):
        try:
            return datetime.strptime(t["transaction_date"], "%m/%d/%Y")
        except Exception:
            return datetime.min

    all_trades.sort(key=_sort_key, reverse=True)
    result = all_trades[:40]

    _ticker_cache[ticker] = (result, time.time())
    return result


# ── Startup warmup ──────────────────────────────────────────────────────────

def warmup_congressional_index() -> None:
    """
    Fetch the FD zip index in a background thread on app startup so the first
    user request doesn't pay the 20-40s cold cost. Silent on failure — if the
    warmup can't reach the House Clerk site, the first user just experiences
    a slower load.
    """
    import threading
    def _run():
        try:
            _fetch_fd_index()
            print("[warmup] FD index preloaded", flush=True)
        except Exception as e:
            print(f"[warmup] FD index preload failed: {e}", flush=True)
    threading.Thread(target=_run, daemon=True).start()
