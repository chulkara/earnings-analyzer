"""
app.py - Flask web app with Server-Sent Events for live progress streaming.

The /analyze route kicks off analysis in a background thread and returns a
job_id. The frontend then connects to /stream/<job_id> via EventSource to
receive live progress messages and the final result.
"""

import os
import json
import time
import threading
import traceback
import concurrent.futures
from datetime import date, timedelta
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from dotenv import load_dotenv
from analyzer import analyze_all_filings, get_cik_for_ticker
from data_sources import get_insider_trades, get_congressional_trades

load_dotenv()
app = Flask(__name__)

# In-memory job store — fine for a single-user local app
_jobs: dict = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Starts analysis in a background thread.
    Returns {"job_id": "..."} immediately so the frontend can open an SSE stream.
    """
    data = request.get_json()
    ticker = (data or {}).get("ticker", "").strip().upper()

    if not ticker or len(ticker) > 10:
        return jsonify({"error": "Please enter a valid ticker symbol."}), 400

    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key or groq_key == "your_groq_api_key_here":
        return jsonify({"error": "GROQ_API_KEY is not set in your .env file."}), 500

    job_id = f"{ticker}_{int(time.time() * 1000)}"
    _jobs[job_id] = {
        "status":   "running",
        "messages": [],
        "result":   None,
        "error":    None,
    }

    def run():
        def on_progress(msg):
            _jobs[job_id]["messages"].append(msg)

        try:
            result = analyze_all_filings(ticker, groq_key, progress=on_progress)
            _jobs[job_id].update({"status": "done", "result": result})
        except ValueError as e:
            _jobs[job_id].update({"status": "error", "error": str(e)})
        except Exception as e:
            print(f"Unexpected error: {e}")
            _jobs[job_id].update({"status": "error", "error": f"Something went wrong: {e}"})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id):
    """
    Server-Sent Events endpoint. Streams progress messages to the browser,
    then sends the final result (or error) and closes.
    """
    def generate():
        seen = 0
        deadline = time.time() + 180  # 3 minute timeout

        while time.time() < deadline:
            job = _jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found.'})}\n\n"
                return

            # Forward any new progress messages
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

        yield f"data: {json.dumps({'type': 'error', 'message': 'Analysis timed out. Please try again.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/trades/<ticker>")
def trades(ticker):
    """
    Returns insider (SEC Form 4) and congressional trading data for a ticker.
    Runs both fetches concurrently. Each fetch fails independently so a problem
    with one source never blocks the other.
    """
    ticker = ticker.upper()
    print(f"[trades] request for {ticker}", flush=True)

    try:
        cik, _ = get_cik_for_ticker(ticker)
    except ValueError as e:
        print(f"[trades] CIK lookup failed: {e}", flush=True)
        return jsonify({"error": str(e)}), 404

    insider_trades  = []
    congress_trades = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        insider_fut  = pool.submit(get_insider_trades, cik)
        congress_fut = pool.submit(get_congressional_trades, ticker)

        try:
            insider_trades = insider_fut.result(timeout=30)
            print(f"[trades] insider: {len(insider_trades)} rows", flush=True)
        except Exception as e:
            print(f"[trades] insider FAILED: {e}", flush=True)
            traceback.print_exc()

        try:
            congress_trades = congress_fut.result(timeout=60)
            print(f"[trades] congressional: {len(congress_trades)} rows", flush=True)
        except Exception as e:
            print(f"[trades] congressional FAILED: {e}", flush=True)
            traceback.print_exc()

    return jsonify({
        "insider":       insider_trades,
        "congressional": congress_trades,
    })


@app.route("/price/<ticker>/<filing_date>")
def price(ticker, filing_date):
    """
    Returns 60 days of daily close prices centred on filing_date (30 before,
    30 after) plus three key stats for the Stock Price Reaction card.

    filing_date must be YYYY-MM-DD.  Uses yfinance; returns [] prices on any
    data error so the frontend can show a graceful empty state.
    """
    try:
        import yfinance as yf
        from datetime import datetime

        filing_dt = datetime.strptime(filing_date, "%Y-%m-%d").date()
        start = filing_dt - timedelta(days=45)   # extra buffer for market holidays
        end   = filing_dt + timedelta(days=45)

        hist = yf.Ticker(ticker).history(start=start.isoformat(),
                                         end=end.isoformat(),
                                         auto_adjust=True)
        if hist.empty:
            return jsonify({"prices": [], "stats": {}})

        # Normalise index to plain date strings
        hist.index = hist.index.tz_localize(None)
        hist = hist[["Close"]].dropna()

        filing_ts  = filing_dt.isoformat()
        dates_list = [d.date().isoformat() for d in hist.index]

        # Find the closest trading day on or after the filing date
        filing_idx = next(
            (i for i, d in enumerate(dates_list) if d >= filing_ts),
            None
        )
        # Limit window: up to 30 trading days before and 30 after the filing
        if filing_idx is not None:
            lo = max(0, filing_idx - 30)
            hi = min(len(dates_list), filing_idx + 31)
            dates_list   = dates_list[lo:hi]
            prices_list  = [round(float(v), 4) for v in hist["Close"].iloc[lo:hi]]
            filing_idx   = filing_idx - lo          # re-index within the window
        else:
            prices_list = [round(float(v), 4) for v in hist["Close"]]

        # Key stats
        stats = {}
        if filing_idx is not None and filing_idx < len(prices_list):
            price_on_date = prices_list[filing_idx]
            price_30_later = prices_list[-1]           # last point in window
            pct_change = ((price_30_later - price_on_date) / price_on_date) * 100
            stats = {
                "price_on_date":   price_on_date,
                "price_30_later":  price_30_later,
                "pct_change":      round(pct_change, 2),
                "filing_date":     dates_list[filing_idx],
                "filing_idx":      filing_idx,
            }

        return jsonify({"prices": list(zip(dates_list, prices_list)), "stats": stats})

    except Exception as e:
        print(f"[price] error for {ticker}/{filing_date}: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"prices": [], "stats": {}})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    debug = os.getenv("FLASK_ENV") != "production"
    print(f"Starting Earnings Analyzer → http://0.0.0.0:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True, use_reloader=False)
