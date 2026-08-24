"""Assumption registry: per-company canonical DCF methodology.

Bridges the FinancialCalc MCP server to the IV-Tracker database, which owns
the assumption_registry table. Provides tools for agents to read prior
assumptions (anchoring new analyses) and register/update them with reasons.
"""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from financialcalc.utils.config import settings
from financialcalc.utils.error_handling import ValidationError
from financialcalc.utils.session_manager import session_manager

logger = logging.getLogger(__name__)

# Canonical ticker forms (must match iv_tracker.config.TICKER_NORMALIZE).
TICKER_NORMALIZE: Dict[str, str] = {
    "BRK-B": "BRK.B",
    "BRK.BRK": "BRK.B",
}

# Terminal-multiple bands by eight-pillar moat percentage.
# (min_percentage_inclusive, max_multiple, min_multiple)
MOAT_RUBRIC: List[Dict[str, Any]] = [
    {"min_pct": 87.5, "band": (14.0, 15.5), "label": "exceptional (7-8/8 pillars)"},
    {"min_pct": 62.5, "band": (11.0, 13.5), "label": "good (5-6/8 pillars)"},
    {"min_pct": 37.5, "band": (9.0, 11.0), "label": "average (3-4/8 pillars)"},
    {"min_pct": 0.0, "band": (8.0, 9.0), "label": "below average (<3/8 pillars)"},
]
DEFAULT_BAND_NO_SCORE = (9.0, 13.0)

REGISTRY_FIELDS = (
    "projection_period",
    "terminal_multiple",
    "discount_rate",
    "base_fcf_method",
    "growth_schedule",
    "moat_score",
)


def normalize_ticker(raw: str) -> str:
    """Normalize a ticker to IV-Tracker canonical form."""
    t = raw.strip().upper()
    return TICKER_NORMALIZE.get(t, t)


def _connect(readonly: bool = True) -> sqlite3.Connection:
    """Open the IV-Tracker database.

    Note: both modes open a read-write connection because the IV-Tracker DB
    uses WAL journal mode; readonly URI connections can fail when no writer
    process holds the -shm file open (e.g., the Streamlit app is closed).
    "readonly" callers only ever execute SELECTs.
    """
    conn = sqlite3.connect(settings.iv_tracker_db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def rubric_band(moat_pct: Optional[float]) -> Dict[str, Any]:
    """Return the terminal-multiple rubric band for a moat percentage."""
    if moat_pct is None:
        return {
            "band": DEFAULT_BAND_NO_SCORE,
            "label": "UNKNOWN (run run_eight_pillar_analysis first)",
        }
    for entry in MOAT_RUBRIC:
        if moat_pct >= entry["min_pct"]:
            return {"band": entry["band"], "label": entry["label"]}
    return {"band": DEFAULT_BAND_NO_SCORE, "label": "UNKNOWN"}


def _get_session_moat_pct(symbol: str) -> Optional[float]:
    """Read the eight-pillar percentage from the session, if analyzed."""
    session = session_manager.get_session(symbol)
    if not session:
        return None
    analysis = session.get("eight_pillar_analysis")
    if not analysis:
        return None
    score = analysis.get("score") or {}
    pct = score.get("percentage")
    return float(pct) if pct is not None else None


def _read_registry_row(ticker: str) -> Optional[Dict[str, Any]]:
    """Read the registry entry for a canonical ticker, or None."""
    try:
        conn = _connect(readonly=True)
    except sqlite3.Error as e:
        logger.warning(f"Cannot open IV-Tracker DB: {e}")
        return None
    try:
        cur = conn.execute(
            "SELECT * FROM assumption_registry WHERE ticker = ?", (ticker,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        entry = dict(row)
        entry["growth_schedule"] = json.loads(entry.get("growth_schedule") or "[]")
        entry["change_log"] = json.loads(entry.get("change_log") or "[]")
        return entry
    except sqlite3.Error as e:
        logger.warning(f"Registry read failed for {ticker}: {e}")
        return None
    finally:
        conn.close()


def _read_smoothed_iv(ticker: str) -> Optional[Dict[str, Any]]:
    """Read best-segment smoothed IV stats for a ticker, if analyses exist."""
    try:
        conn = _connect(readonly=True)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT intrinsic_value, projection_period, data_anomaly, excluded "
            "FROM analysis WHERE ticker = ? ORDER BY timestamp",
            (ticker,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    clean = [
        r
        for r in rows
        if r["intrinsic_value"] is not None
        and not r["data_anomaly"]
        and not r["excluded"]
    ]
    if not clean:
        return None

    periods: Dict[int, List[float]] = {}
    for r in clean:
        periods.setdefault(r["projection_period"] or 0, []).append(r["intrinsic_value"])

    best_period = max(periods, key=lambda p: len(periods[p]))
    values = periods[best_period][-5:]
    values_sorted = sorted(values)
    n = len(values_sorted)
    median = (
        values_sorted[n // 2]
        if n % 2 == 1
        else (values_sorted[n // 2 - 1] + values_sorted[n // 2]) / 2
    )
    return {
        "smoothed_iv": round(median, 2),
        "based_on_last": n,
        "projection_period": best_period or None,
        "segment_sizes": {str(k): len(v) for k, v in periods.items()},
    }


def get_prior_assumptions(ticker: str) -> Dict[str, Any]:
    """Read prior registered assumptions and history stats for a ticker.

    Returns the registry entry (if any), smoothed IV from analysis history,
    the moat-based terminal-multiple rubric, and the suggested discount-rate
    baseline (registry value if present). Agents MUST reuse registered
    assumptions for re-analyses; changes require a documented reason.
    """
    canonical = normalize_ticker(ticker)
    entry = _read_registry_row(canonical)
    smoothed = _read_smoothed_iv(canonical)
    moat_pct = _get_session_moat_pct(canonical) or (
        entry.get("moat_score") if entry else None
    )
    rubric = rubric_band(moat_pct)

    result: Dict[str, Any] = {
        "ticker": canonical,
        "registered": entry is not None,
        "assumptions": entry,
        "smoothed_iv": smoothed,
        "moat_pct": moat_pct,
        "terminal_multiple_rubric": {
            "band": list(rubric["band"]),
            "quality_label": rubric["label"],
        },
        "guidance": (
            "Reuse the registered assumptions exactly for re-analysis. To change "
            "any lever, you must pass override_reason to the DCF tool citing a "
            "changed fundamental, then call register_assumptions to update the "
            "registry."
            if entry
            else "No registry entry. Run run_eight_pillar_analysis, pick the "
            "terminal multiple within the rubric band, the discount rate near "
            "the WACC baseline, then call register_assumptions after the DCF."
        ),
    }

    if entry is None:
        result["suggested_discount_rate"] = None
    else:
        result["suggested_discount_rate"] = entry["discount_rate"]

    return result


def register_assumptions(
    ticker: str,
    projection_period: int,
    terminal_multiple: float,
    discount_rate: float,
    base_fcf_method: str = "median",
    growth_schedule: Optional[List[float]] = None,
    moat_pct: Optional[float] = None,
    reason: str = "initial registration",
) -> Dict[str, Any]:
    """Register or update the canonical DCF assumptions for a ticker.

    Validates inputs against the moat rubric (when a score is available) and
    writes to the IV-Tracker registry with a change log. Updates require a
    reason; every field change is recorded with old/new values.
    """
    canonical = normalize_ticker(ticker)

    if projection_period not in (5, 10):
        raise ValidationError("projection_period must be 5 or 10")
    if not (5.0 <= terminal_multiple <= 20.0):
        raise ValidationError("terminal_multiple must be between 5 and 20")
    if not (5.0 <= discount_rate <= 16.0):
        raise ValidationError("discount_rate must be between 5 and 16 percent")
    if base_fcf_method not in (
        "most_recent",
        "average",
        "median",
        "sbc_adjusted",
        "normalized",
    ):
        raise ValidationError(f"Unknown base_fcf_method: {base_fcf_method}")

    if moat_pct is None:
        moat_pct = _get_session_moat_pct(canonical)

    # Rubric check: reject multiples outside the band on NEW registrations.
    # Updates may move within ±1.5x of the existing value with a reason.
    existing = _read_registry_row(canonical)
    if existing is None:
        rubric = rubric_band(moat_pct)
        lo, hi = rubric["band"]
        if moat_pct is not None and not (lo <= terminal_multiple <= hi):
            raise ValidationError(
                f"terminal_multiple {terminal_multiple} outside moat rubric band "
                f"[{lo}, {hi}] for quality '{rubric['label']}'. Re-run with a "
                "justified multiple inside the band."
            )
    else:
        is_default_reason = (
            not reason
            or reason.strip() == ""
            or reason.strip() == "initial registration"
        )
        old_tm = existing["terminal_multiple"]
        old_dr = existing["discount_rate"]
        old_period = existing["projection_period"]
        substantive_change = (
            abs(terminal_multiple - old_tm) > 0.01
            or abs(discount_rate - old_dr) > 0.01
            or projection_period != old_period
        )
        if substantive_change and is_default_reason:
            raise ValidationError(
                "Updating registry methodology requires a substantive reason "
                "citing the changed fundamental (got default/empty reason)"
            )

    schedule = [round(float(g), 2) for g in (growth_schedule or [])]
    now = datetime.now().isoformat(timespec="seconds")

    try:
        conn = _connect(readonly=False)
    except sqlite3.Error as e:
        raise ValidationError(f"Cannot open IV-Tracker DB for writing: {e}")

    try:
        if existing is None:
            conn.execute(
                "INSERT INTO assumption_registry (ticker, projection_period, "
                "terminal_multiple, discount_rate, base_fcf_method, growth_schedule, "
                "moat_score, locked_at, change_log) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical,
                    projection_period,
                    terminal_multiple,
                    discount_rate,
                    base_fcf_method,
                    json.dumps(schedule),
                    moat_pct,
                    now,
                    json.dumps([]),
                ),
            )
            conn.commit()
            action = "created"
            changes: List[Dict[str, Any]] = []
        else:
            if not reason or not reason.strip():
                raise ValidationError("A reason is required to update the registry")
            changes = []
            # Omitted optional fields mean "leave unchanged": moat_score None
            # and empty growth_schedule never overwrite existing values.
            new_values: Dict[str, Any] = {
                "projection_period": projection_period,
                "terminal_multiple": terminal_multiple,
                "discount_rate": discount_rate,
                "base_fcf_method": base_fcf_method,
            }
            if schedule:
                new_values["growth_schedule"] = schedule
            if moat_pct is not None:
                new_values["moat_score"] = moat_pct
            updates: Dict[str, Any] = {}
            for field, new_value in new_values.items():
                old_value = existing.get(field)
                if field == "growth_schedule":
                    old_cmp = old_value if isinstance(old_value, list) else []
                    if old_cmp != schedule:
                        updates[field] = json.dumps(schedule)
                        changes.append(
                            {
                                "changed_at": now,
                                "field": field,
                                "old": old_cmp,
                                "new": schedule,
                                "reason": reason,
                            }
                        )
                elif old_value != new_value:
                    updates[field] = new_value
                    changes.append(
                        {
                            "changed_at": now,
                            "field": field,
                            "old": old_value,
                            "new": new_value,
                            "reason": reason,
                        }
                    )
            if updates:
                set_clause = ", ".join(f"{f} = ?" for f in updates)
                params = list(updates.values()) + [now, canonical]
                conn.execute(
                    f"UPDATE assumption_registry SET {set_clause}, locked_at = ? "
                    "WHERE ticker = ?",
                    params,
                )
                log = existing.get("change_log") or []
                log = list(log) + changes
                conn.execute(
                    "UPDATE assumption_registry SET change_log = ? WHERE ticker = ?",
                    (json.dumps(log), canonical),
                )
                conn.commit()
            action = "updated"
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise ValidationError(f"Registry write failed: {e}")
    finally:
        conn.close()

    entry = _read_registry_row(canonical)
    return {
        "ticker": canonical,
        "action": action,
        "assumptions": entry,
        "changes": changes,
    }
