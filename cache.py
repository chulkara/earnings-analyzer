"""
cache.py - SQLite-backed cache for filings, LLM analyses, and run history.

Backend is auto-selected:
  - If TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set  → libsql embedded
    replica syncing to Turso (free tier: 9 GB storage, 1B reads, 25M writes).
  - Otherwise                                          → plain sqlite3 file.

Both backends expose the same DB-API surface, so the rest of the module
doesn't care which one is running. Rows come back as tuples; a small
`_row_to_dict` helper handles both.

Three tables:
  - filings   : raw SEC filing content, keyed by (cik, accession).
  - analyses  : LLM outputs, keyed by (content_hash, prompt_version, model).
  - runs      : one row per /analyze job. Powers permalinks.
  - transcripts: earnings call transcripts by (symbol, quarter).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Optional


# ─── Configuration ───────────────────────────────────────────────────────────
DEFAULT_DB_PATH = os.getenv("CACHE_DB_PATH", "cache.db")


# ─── Schema ─────────────────────────────────────────────────────────────────
_SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS filings (
        cik          TEXT NOT NULL,
        accession    TEXT NOT NULL,
        form         TEXT,
        date         TEXT,
        primary_doc  TEXT,
        content      TEXT NOT NULL,
        fetched_at   REAL NOT NULL,
        PRIMARY KEY (cik, accession)
    )""",
    """CREATE TABLE IF NOT EXISTS analyses (
        content_hash    TEXT NOT NULL,
        prompt_version  TEXT NOT NULL,
        model           TEXT NOT NULL,
        analysis_json   TEXT NOT NULL,
        analyzed_at     REAL NOT NULL,
        PRIMARY KEY (content_hash, prompt_version, model)
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id       TEXT PRIMARY KEY,
        ticker       TEXT NOT NULL,
        result_json  TEXT NOT NULL,
        created_at   REAL NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_runs_ticker_created
        ON runs (ticker, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS transcripts (
        symbol         TEXT NOT NULL,
        quarter        TEXT NOT NULL,
        transcript_json TEXT NOT NULL,
        fetched_at     REAL NOT NULL,
        PRIMARY KEY (symbol, quarter)
    )""",
]


# Column order matches the CREATE statements above — used to build dicts
# from tuple rows when the driver doesn't provide row_factory.
_COLUMNS = {
    "filings":     ["cik", "accession", "form", "date", "primary_doc", "content", "fetched_at"],
    "analyses":    ["content_hash", "prompt_version", "model", "analysis_json", "analyzed_at"],
    "runs":        ["run_id", "ticker", "result_json", "created_at"],
    "transcripts": ["symbol", "quarter", "transcript_json", "fetched_at"],
}


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _row_to_dict(row, columns):
    """Handles both sqlite3.Row and plain tuples."""
    if row is None:
        return None
    if hasattr(row, "keys"):        # sqlite3.Row
        return {k: row[k] for k in row.keys()}
    return dict(zip(columns, row))  # plain tuple (libsql)


# ─── Connection factory ─────────────────────────────────────────────────────

def _open_connection(db_path: str):
    """
    Returns (connection, is_libsql). If TURSO_* env vars are set, we open
    a libsql embedded replica that syncs to the remote Turso DB; otherwise
    plain sqlite3.

    Both backends support .execute() with '?' placeholders and cursor
    .fetchone()/.fetchall() returning tuples or Rows.
    """
    turso_url   = os.getenv("TURSO_DATABASE_URL", "").strip()
    turso_token = os.getenv("TURSO_AUTH_TOKEN",   "").strip()

    if turso_url and turso_token:
        try:
            import libsql_experimental as libsql   # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TURSO_DATABASE_URL is set but libsql-experimental is not "
                "installed. Add `libsql-experimental` to requirements.txt."
            ) from e
        conn = libsql.connect(
            db_path,
            sync_url=turso_url,
            auth_token=turso_token,
        )
        try:
            conn.sync()   # pull any changes into the local replica
        except Exception:
            pass          # first-run replicas may not have a remote yet
        return conn, True

    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn, False


# ─── Cache ──────────────────────────────────────────────────────────────────

class Cache:
    """
    Thin wrapper around a SQLite / libsql connection.
    Thread-safe via a per-instance lock.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn, self._is_libsql = _open_connection(db_path)

        # Run schema — libsql doesn't have executescript, so run one at a time.
        for stmt in _SCHEMA_STATEMENTS:
            self._conn.execute(stmt)
        self._commit_if_needed()

    # ─── libsql needs explicit commits; sqlite3 in isolation_level=None
    # ─── autocommits. Handle both.
    def _commit_if_needed(self):
        if self._is_libsql:
            try:
                self._conn.commit()
            except Exception:
                pass

    def _sync_if_needed(self):
        """Push local writes up to the Turso remote (libsql only)."""
        if self._is_libsql:
            try:
                self._conn.sync()
            except Exception:
                pass

    # ─── Filings ────────────────────────────────────────────────────────────

    def get_filing(self, cik: str, accession: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT cik, accession, form, date, primary_doc, content, fetched_at "
                "FROM filings WHERE cik = ? AND accession = ?",
                (cik, accession),
            )
            row = cur.fetchone()
        return _row_to_dict(row, _COLUMNS["filings"])

    def put_filing(self, cik, accession, form, date, primary_doc, content) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO filings
                   (cik, accession, form, date, primary_doc, content, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (cik, accession, form, date, primary_doc, content, time.time()),
            )
            self._commit_if_needed()
        self._sync_if_needed()

    # ─── Analyses ───────────────────────────────────────────────────────────

    def get_analysis(self, content, prompt_version, model) -> Optional[dict]:
        h = _hash_content(content)
        with self._lock:
            cur = self._conn.execute(
                """SELECT analysis_json FROM analyses
                   WHERE content_hash = ? AND prompt_version = ? AND model = ?""",
                (h, prompt_version, model),
            )
            row = cur.fetchone()
        if not row:
            return None
        raw = row[0] if not hasattr(row, "keys") else row["analysis_json"]
        return json.loads(raw)

    def put_analysis(self, content, prompt_version, model, analysis) -> None:
        h = _hash_content(content)
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO analyses
                   (content_hash, prompt_version, model, analysis_json, analyzed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (h, prompt_version, model, json.dumps(analysis), time.time()),
            )
            self._commit_if_needed()
        self._sync_if_needed()

    # ─── Runs (permalinks) ──────────────────────────────────────────────────

    def record_run(self, ticker: str, result: dict) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                """INSERT INTO runs (run_id, ticker, result_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (run_id, ticker.upper(), json.dumps(result), time.time()),
            )
            self._commit_if_needed()
        self._sync_if_needed()
        return run_id

    def list_runs(self, ticker: str, limit: int = 20) -> list:
        with self._lock:
            cur = self._conn.execute(
                """SELECT run_id, ticker, created_at, result_json FROM runs
                   WHERE ticker = ? ORDER BY created_at DESC LIMIT ?""",
                (ticker.upper(), limit),
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            if hasattr(r, "keys"):
                run_id, tkr, created_at, result_json = \
                    r["run_id"], r["ticker"], r["created_at"], r["result_json"]
            else:
                run_id, tkr, created_at, result_json = r
            res = json.loads(result_json)
            out.append({
                "run_id":     run_id,
                "ticker":     tkr,
                "created_at": created_at,
                "form":       res.get("form"),
                "date":       res.get("date"),
                "sentiment":  res.get("sentiment"),
                "sentiment_score": res.get("sentiment_score"),
            })
        return out

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT run_id, ticker, result_json, created_at FROM runs WHERE run_id = ?",
                (run_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        if hasattr(row, "keys"):
            rid, tkr, result_json, created_at = \
                row["run_id"], row["ticker"], row["result_json"], row["created_at"]
        else:
            rid, tkr, result_json, created_at = row
        return {
            "run_id":     rid,
            "ticker":     tkr,
            "result":     json.loads(result_json),
            "created_at": created_at,
        }

    # ─── Transcripts ────────────────────────────────────────────────────────

    def get_transcript(self, symbol: str, quarter: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT transcript_json FROM transcripts WHERE symbol = ? AND quarter = ?",
                (symbol.upper(), quarter),
            )
            row = cur.fetchone()
        if not row:
            return None
        raw = row[0] if not hasattr(row, "keys") else row["transcript_json"]
        return json.loads(raw)

    def put_transcript(self, symbol: str, quarter: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO transcripts
                   (symbol, quarter, transcript_json, fetched_at)
                   VALUES (?, ?, ?, ?)""",
                (symbol.upper(), quarter, json.dumps(payload), time.time()),
            )
            self._commit_if_needed()
        self._sync_if_needed()

    # ─── Utility ────────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ─── Module-level singleton ─────────────────────────────────────────────────
_default_cache: Optional[Cache] = None


def get_cache() -> Cache:
    global _default_cache
    if _default_cache is None:
        _default_cache = Cache()
    return _default_cache


@contextmanager
def temporary_cache(path: str):
    """Tests point at a throwaway sqlite file (never Turso)."""
    # Force sqlite3 even if Turso env vars are set in the shell.
    saved = {k: os.environ.pop(k, None) for k in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")}
    try:
        c = Cache(db_path=path)
        try:
            yield c
        finally:
            c.close()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
