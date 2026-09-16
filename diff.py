"""
diff.py - Compare two analysis result payloads and describe what changed.

Two analyses are "different" in ways users actually care about:
  - sentiment moved (score delta)
  - beats_estimates flipped
  - key themes changed (added / removed)
  - new risks appeared, old risks dropped off
  - new analyst concerns

We keep the comparison shallow — string equality on themes/risks — since
the LLM's phrasing is stable enough within a prompt version to make that
signal meaningful, and any nuance beyond that belongs in a fresh analysis.
"""

from __future__ import annotations
from typing import Optional


def _title_set(themes):
    return {(t.get("title") or "").strip().lower()
            for t in (themes or []) if isinstance(t, dict)}


def _themes_by_title(themes):
    return {(t.get("title") or "").strip().lower(): t
            for t in (themes or []) if isinstance(t, dict)}


def _norm_list(items):
    return [(s or "").strip() for s in (items or []) if (s or "").strip()]


def diff_analyses(newer: dict, older: dict) -> dict:
    """
    Build a structured diff. `newer` and `older` are full run results
    (as stored by cache.record_run). Both must be present.
    """
    # ─── Sentiment shift ───────────────────────────────────────────────
    new_score = float(newer.get("sentiment_score") or 0.0)
    old_score = float(older.get("sentiment_score") or 0.0)
    score_delta = round(new_score - old_score, 3)

    sentiment_shift = {
        "old_score":  old_score,
        "new_score":  new_score,
        "delta":      score_delta,
        "old_label":  newer.get("sentiment") if False else older.get("sentiment"),
        "new_label":  newer.get("sentiment"),
        "flipped":    older.get("sentiment") != newer.get("sentiment"),
    }

    # ─── Beat/miss flip ────────────────────────────────────────────────
    beat_flip = None
    if older.get("beats_estimates") is not None and newer.get("beats_estimates") is not None:
        if older["beats_estimates"] != newer["beats_estimates"]:
            beat_flip = {
                "was":     bool(older["beats_estimates"]),
                "now":     bool(newer["beats_estimates"]),
            }

    # ─── Themes: added / removed ───────────────────────────────────────
    new_titles = _title_set(newer.get("key_themes"))
    old_titles = _title_set(older.get("key_themes"))
    added_titles   = new_titles - old_titles
    removed_titles = old_titles - new_titles

    new_by_title = _themes_by_title(newer.get("key_themes"))
    old_by_title = _themes_by_title(older.get("key_themes"))

    themes_added   = [new_by_title[t] for t in added_titles   if t in new_by_title]
    themes_removed = [old_by_title[t] for t in removed_titles if t in old_by_title]

    # ─── Risks: added / removed ────────────────────────────────────────
    new_risks = set(_norm_list(newer.get("risks")))
    old_risks = set(_norm_list(older.get("risks")))
    risks_added   = sorted(new_risks - old_risks)
    risks_removed = sorted(old_risks - new_risks)

    # ─── New analyst concerns (only present when transcript was used) ──
    new_concerns = set(_norm_list(newer.get("analyst_concerns")))
    old_concerns = set(_norm_list(older.get("analyst_concerns")))
    concerns_added = sorted(new_concerns - old_concerns)

    # ─── Summary sentence for the diff header ──────────────────────────
    parts = []
    if abs(score_delta) >= 0.1:
        direction = "up" if score_delta > 0 else "down"
        parts.append(f"Sentiment {direction} {abs(score_delta):.2f}")
    if beat_flip:
        parts.append("beats/misses flipped")
    if themes_added:
        parts.append(f"{len(themes_added)} new theme{'s' if len(themes_added) != 1 else ''}")
    if risks_added:
        parts.append(f"{len(risks_added)} new risk{'s' if len(risks_added) != 1 else ''}")
    headline = " · ".join(parts) if parts else "No material changes"

    return {
        "headline":        headline,
        "sentiment_shift": sentiment_shift,
        "beat_flip":       beat_flip,
        "themes_added":    themes_added,
        "themes_removed":  themes_removed,
        "risks_added":     risks_added,
        "risks_removed":   risks_removed,
        "concerns_added":  concerns_added,
        "meta": {
            "newer": {
                "form": newer.get("form"),
                "date": newer.get("date"),
            },
            "older": {
                "form": older.get("form"),
                "date": older.get("date"),
            },
        },
    }
