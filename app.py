"""
app.py - Flask web app with Server-Sent Events for live progress streaming,
plus permalinks + diff mode backed by the SQLite cache in cache.py.

Route map:
  /                        landing page
  /r/<run_id>              landing page pre-loaded with a saved run
  /analyze         POST    starts an analysis job → returns {job_id}
  /stream/<job_id>         SSE stream of progress + final result
  /run/<run_id>            fetch a saved run's full result (JSON)
  /history/<ticker>        last N runs for a ticker (for the diff picker)
  /diff/<a>/<b>            structured diff between two runs
  /trades/<ticker>         insider + congressional trades
  /price/<ticker>/<date>   ±30-day price series around a filing date
"""

import os
import json
import time
import threading
import traceback
import concurrent.futures
from datetime import date, timedelta

from flask import (
    Flask, render_template, request, jsonify, Response,
    stream_with_context, abort, url_for,
)
from dotenv import load_dotenv

from analyzer import analyze_all_filings, get_cik_for_ticker
from data_sources import (
    get_insider_trades,
    get_congressional_trades,
    warmup_congressional_index,
)
from cache import get_cache
from diff import diff_analyses


load_dotenv()
app = Flask(__name__)

# In-memory job store — SSE state, not the source of truth
_jobs: dict = {}

# Fire and forget: preload the House Clerk FD index in the background so the
# first user's /trades request doesn't pay the ~20-40s cold cost.
warmup_congressional_index()


# ─── Demo whitelist ─────────────────────────────────────────────────────────
# When DEMO_TICKERS is set (e.g. "AAPL,NVDA,TSLA,MSFT,JPM") we only allow
# those tickers. Empty / unset = open to everything.
def _demo_tickers() -> set:
    raw = os.getenv("DEMO_TICKERS", "").strip()
    if not raw:
        return set()
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


def _demo_allowed(ticker: str) -> bool:
    whitelist = _demo_tickers()
    return not whitelist or ticker.upper() in whitelist


# ─── Pages ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        preloaded_run=None,
        demo_tickers=sorted(_demo_tickers()),
    )


@app.route("/r/<run_id>")
def index_with_run(run_id):
    """Landing page pre-loaded with a previously saved run."""
    cache = get_cache()
    run = cache.get_run(run_id)
    if not run:
        abort(404)
    return render_template(
        "index.html",
        preloaded_run=run,
        demo_tickers=sorted(_demo_tickers()),
    )


# ─── Analysis ───────────────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    ticker = (data or {}).get("ticker", "").strip().upper()

    if not ticker or len(ticker) > 10:
        return jsonify({"error": "Please enter a valid ticker symbol."}), 400

    if not _demo_allowed(ticker):
        allowed = ", ".join(sorted(_demo_tickers()))
        return jsonify({
            "error": f"Demo is limited to: {allowed}. "
                     "Clone the repo and set your own GROQ_API_KEY to run anything."
        }), 403

    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key or groq_key == "your_groq_api_key_here":
        return jsonify({"error": "GROQ_API_KEY is not set."}), 500

    job_id = f"{ticker}_{int(time.time() * 1000)}"
    _jobs[job_id] = {"status": "running", "messages": [], "result": None, "error": None}

    def run():
        def on_progress(msg):
            _jobs[job_id]["messages"].append(msg)
        try:
            result = analyze_all_filings(ticker, groq_key, progress=on_progress)
            # Persist the run so we get a permalink + can diff against it later.
            run_id = get_cache().record_run(ticker, result)
            result["run_id"]  = run_id
            result["permalink"] = f"/r/{run_id}"
            _jobs[job_id].update({"status": "done", "result": result})
        except ValueError as e:
            _jobs[job_id].update({"status": "error", "error": str(e)})
        except Exception as e:
            print(f"Unexpected error: {e}")
            traceback.print_exc()
            _jobs[job_id].update({"status": "error", "error": f"Something went wrong: {e}"})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id):
    def generate():
        seen = 0
        deadline = time.time() + 180
        while time.time() < deadline:
            job = _jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found.'})}\n\n"
                return
            msgs = job["messages"]
            while seen < len(msgs):
                yield f"data: {json.dumps({'type': 'progress', 'message': msgs[seen]})}\n\n"
                seen += 1
            if job["status"] == "done":
                yield f"data: {json.dumps({'type': 'done', 'result': job['result']})}\n\n"
                _jobs.pop(job_id, None)
                return
            if job["status"] == "error":
                yield f"data: {json.dumps({'type': 'error', 'message': job['error']})}\n\n"
                _jobs.pop(job_id, None)
                return
            time.sleep(0.25)
        yield f"data: {json.dumps({'type': 'error', 'message': 'Analysis timed out.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Permalinks & diff ──────────────────────────────────────────────────────

@app.route("/run/<run_id>")
def get_run(run_id):
    run = get_cache().get_run(run_id)
    if not run:
        return jsonify({"error": "Run not found."}), 404
    return jsonify(run)


@app.route("/history/<ticker>")
def history(ticker):
    runs = get_cache().list_runs(ticker.upper(), limit=20)
    return jsonify({"ticker": ticker.upper(), "runs": runs})


@app.route("/diff/<run_a>/<run_b>")
def diff_route(run_a, run_b):
    """
    Compare run_a (newer) against run_b (older). Both must exist.
    Returns a structured diff. Order matters — the frontend chooses.
    """
    cache = get_cache()
    a = cache.get_run(run_a)
    b = cache.get_run(run_b)
    if not a or not b:
        return jsonify({"error": "One or both runs not found."}), 404
    return jsonify({
        "diff":  diff_analyses(a["result"], b["result"]),
        "newer": {"run_id": run_a, "created_at": a["created_at"]},
        "older": {"run_id": run_b, "created_at": b["created_at"]},
    })


# ─── Trades ─────────────────────────────────────────────────────────────────

@app.route("/trades/<ticker>")
def trades(ticker):
    ticker = ticker.upper()
    try:
        cik, _ = get_cik_for_ticker(ticker)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    insider_trades  = []
    congress_trades = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        insider_fut  = pool.submit(get_insider_trades, cik)
        congress_fut = pool.submit(get_congressional_trades, ticker)
        try:
            insider_trades = insider_fut.result(timeout=20)
        except Exception as e:
            print(f"[trades] insider FAILED: {e}", flush=True)
            traceback.print_exc()
        try:
            congress_trades = congress_fut.result(timeout=25)
        except Exception as e:
            print(f"[trades] congressional FAILED: {e}", flush=True)
            traceback.print_exc()

    return jsonify({"insider": insider_trades, "congressional": congress_trades})


# ─── Price series ───────────────────────────────────────────────────────────

@app.route("/price/<ticker>/<filing_date>")
def price(ticker, filing_date):
    try:
        import yfinance as yf
        from datetime import datetime

        filing_dt = datetime.strptime(filing_date, "%Y-%m-%d").date()
        start = filing_dt - timedelta(days=45)
        end   = filing_dt + timedelta(days=45)

        hist = yf.Ticker(ticker).history(start=start.isoformat(),
                                         end=end.isoformat(),
                                         auto_adjust=True)
        if hist.empty:
            return jsonify({"prices": [], "stats": {}})

        hist.index = hist.index.tz_localize(None)
        hist = hist[["Close"]].dropna()

        filing_ts  = filing_dt.isoformat()
        dates_list = [d.date().isoformat() for d in hist.index]

        filing_idx = next(
            (i for i, d in enumerate(dates_list) if d >= filing_ts), None
        )
        if filing_idx is not None:
            lo = max(0, filing_idx - 30)
            hi = min(len(dates_list), filing_idx + 31)
            dates_list  = dates_list[lo:hi]
            prices_list = [round(float(v), 4) for v in hist["Close"].iloc[lo:hi]]
            filing_idx  = filing_idx - lo
        else:
            prices_list = [round(float(v), 4) for v in hist["Close"]]

        stats = {}
        if filing_idx is not None and filing_idx < len(prices_list):
            price_on_date  = prices_list[filing_idx]
            price_30_later = prices_list[-1]
            pct_change = ((price_30_later - price_on_date) / price_on_date) * 100
            stats = {
                "price_on_date":  price_on_date,
                "price_30_later": price_30_later,
                "pct_change":     round(pct_change, 2),
                "filing_date":    dates_list[filing_idx],
                "filing_idx":     filing_idx,
            }

        return jsonify({"prices": list(zip(dates_list, prices_list)), "stats": stats})

    except Exception as e:
        print(f"[price] error for {ticker}/{filing_date}: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"prices": [], "stats": {}})


# ─── Health check (for Fly.io) ──────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "cache_db": os.getenv("CACHE_DB_PATH", "cache.db")})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    debug = os.getenv("FLASK_ENV") != "production"
    print(f"Starting Earnings Analyzer → http://0.0.0.0:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True, use_reloader=False)
