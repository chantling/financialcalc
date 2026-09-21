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

# Justified price-to-book bands by moat percentage for residual-income
# (financial company) methodologies.
FINANCIALS_MOAT_RUBRIC: List[Dict[str, Any]] = [
    {"min_pct": 87.5, "band": (1.6, 2.0), "label": "exceptional (7-8/8 pillars)"},
    {"min_pct": 62.5, "band": (1.2, 1.6), "label": "good (5-6/8 pillars)"},
    {"min_pct": 37.5, "band": (0.9, 1.2), "label": "average (3-4/8 pillars)"},
    {"min_pct": 0.0, "band": (0.7, 1.0), "label": "below average (<3/8 pillars)"},
]
DEFAULT_FINANCIALS_BAND_NO_SCORE = (0.9, 1.5)

REGISTRY_METHODS = ("dcf", "residual_income")

REGISTRY_FIELDS = (
    "method",
    "projection_period",
    "terminal_multiple",
    "discount_rate",
    "base_fcf_method",
    "growth_schedule",
    "roe_schedule",
    "payout_ratio",
    "terminal_growth",
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
    return _band_for_rubric(MOAT_RUBRIC, DEFAULT_BAND_NO_SCORE, moat_pct)


def financials_rubric_band(moat_pct: Optional[float]) -> Dict[str, Any]:
    """Return the justified-P/B rubric band for a moat percentage."""
    return _band_for_rubric(
        FINANCIALS_MOAT_RUBRIC, DEFAULT_FINANCIALS_BAND_NO_SCORE, moat_pct
    )


def _band_for_rubric(
    rubric: List[Dict[str, Any]],
    default_band: tuple,
    moat_pct: Optional[float],
) -> Dict[str, Any]:
    """Resolve a moat percentage against a rubric (shared implementation)."""
    if moat_pct is None:
        return {
            "band": default_band,
            "label": "UNKNOWN (run run_eight_pillar_analysis first)",
        }
    for entry in rubric:
        if moat_pct >= entry["min_pct"]:
            return {"band": entry["band"], "label": entry["label"]}
    return {"band": default_band, "label": "UNKNOWN"}


def _ensure_method_columns(conn: sqlite3.Connection) -> None:
    """Migrate the registry to the method-aware schema (idempotent).

    The IV-Tracker DB owns the assumption_registry table; older installs
    lack the method discriminator and residual-income fields, and mark
    terminal_multiple/base_fcf_method NOT NULL (which residual-income
    rows do not use). When any new column is missing the table is
    rebuilt: all columns present, new columns added with defaults
    (existing rows become method='dcf'), and the two DCF-only fields
    relaxed to nullable. Anything else is left untouched.
    """
    required = {"method", "roe_schedule", "payout_ratio", "terminal_growth"}
    try:
        cur = conn.execute("PRAGMA table_info(assumption_registry)")
        col_names = {row[1] for row in cur.fetchall()}
        if required <= col_names:
            return
        col_list = ", ".join(sorted(col_names))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS assumption_registry_new (
                ticker            TEXT PRIMARY KEY,
                projection_period INTEGER NOT NULL,
                terminal_multiple REAL,
                discount_rate     REAL NOT NULL,
                base_fcf_method   TEXT,
                growth_schedule   TEXT NOT NULL,
                method            TEXT NOT NULL DEFAULT 'dcf',
                roe_schedule      TEXT NOT NULL DEFAULT '[]',
                payout_ratio      REAL,
                terminal_growth   REAL,
                moat_score        REAL,
                locked_at         TEXT NOT NULL,
                change_log        TEXT NOT NULL DEFAULT '[]'
            );
            INSERT INTO assumption_registry_new (
                ticker, projection_period, terminal_multiple, discount_rate,
                base_fcf_method, growth_schedule, method, roe_schedule,
                payout_ratio, terminal_growth, moat_score, locked_at,
                change_log
            )
            SELECT ticker, projection_period, terminal_multiple, discount_rate,
                base_fcf_method, growth_schedule, 'dcf', '[]', NULL, NULL,
                moat_score, locked_at, change_log
            FROM assumption_registry;
            DROP TABLE assumption_registry;
            ALTER TABLE assumption_registry_new RENAME TO assumption_registry;
            """)
        conn.commit()
        logger.info(
            f"Migrated assumption_registry to method-aware schema "
            f"(preserved columns: {col_list})"
        )
    except sqlite3.Error as e:
        logger.warning(f"Registry migration check failed: {e}")


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


def _get_session_financials_moat_pct(symbol: str) -> Optional[float]:
    """Read the financials-variant pillar percentage from the session."""
    session = session_manager.get_session(symbol)
    if not session:
        return None
    analysis = session.get("financials_pillar_analysis")
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
        entry["roe_schedule"] = json.loads(entry.get("roe_schedule") or "[]")
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
    the moat-based rubric (terminal multiple for DCF entries, justified
    P/B for residual_income entries), and the suggested discount-rate
    baseline (registry value if present). Agents MUST reuse registered
    assumptions for re-analyses; changes require a documented reason.
    """
    canonical = normalize_ticker(ticker)
    entry = _read_registry_row(canonical)
    smoothed = _read_smoothed_iv(canonical)
    method = (entry or {}).get("method") or "dcf"

    if method == "residual_income":
        moat_pct = _get_session_financials_moat_pct(canonical) or (
            entry.get("moat_score") if entry else None
        )
        rubric = financials_rubric_band(moat_pct)
        rubric_key = "justified_pb_rubric"
        tool_name = "run_financials_valuation"
    else:
        moat_pct = _get_session_moat_pct(canonical) or (
            entry.get("moat_score") if entry else None
        )
        rubric = rubric_band(moat_pct)
        rubric_key = "terminal_multiple_rubric"
        tool_name = "run_dcf_analysis"

    result: Dict[str, Any] = {
        "ticker": canonical,
        "registered": entry is not None,
        "method": method,
        "assumptions": entry,
        "smoothed_iv": smoothed,
        "moat_pct": moat_pct,
        rubric_key: {
            "band": list(rubric["band"]),
            "quality_label": rubric["label"],
        },
        "guidance": (
            f"Reuse the registered {method} assumptions exactly for "
            "re-analysis. To change any lever, you must pass override_reason "
            f"to {tool_name} citing a changed fundamental, then call "
            "register_assumptions to update the registry."
            if entry
            else "No registry entry. Run the pillar analysis "
            f"({'run_financials_pillar_analysis' if method == 'residual_income' else 'run_eight_pillar_analysis'}), "
            f"pick assumptions within the rubric band ({rubric_key}), then "
            "call register_assumptions after the valuation."
        ),
    }

    if entry is None:
        result["suggested_discount_rate"] = None
    else:
        result["suggested_discount_rate"] = entry["discount_rate"]

    # Stale-methodology warning: a registered residual-income entry for a
    # company the asset-light override reclassifies as operating must not
    # be reused (it produced book-anchored IVs far below market).
    if entry is not None and method == "residual_income":
        session = session_manager.get_session(canonical)
        if session:
            from financialcalc.tools.financials_valuation import (
                classify_valuation_track,
            )

            classification = classify_valuation_track(
                session.get("financial_data"),
                session.get("balance_sheet"),
                session.get("current_metrics"),
            )
            if classification["asset_light_override"]:
                result["valuation_track"] = classification
                result["guidance"] = (
                    f"WARNING: {classification['reason']} The registered "
                    "residual-income methodology for this ticker is stale "
                    "(book-anchored; it cannot capture this business). Do "
                    "NOT reuse it: value with run_dcf_analysis instead "
                    "(first-run guardrails apply), then call "
                    "register_assumptions with method='dcf' to replace the "
                    "stale entry."
                )

    return result


def register_assumptions(
    ticker: str,
    projection_period: int,
    terminal_multiple: Optional[float] = None,
    discount_rate: float = 10.0,
    base_fcf_method: str = "median",
    growth_schedule: Optional[List[float]] = None,
    moat_pct: Optional[float] = None,
    reason: str = "initial registration",
    method: str = "dcf",
    roe_schedule: Optional[List[float]] = None,
    payout_ratio: Optional[float] = None,
    terminal_growth: Optional[float] = None,
) -> Dict[str, Any]:
    """Register or update the canonical assumptions for a ticker.

    Supports two methodologies:
        - method="dcf" (default): FCF DCF assumptions (terminal_multiple,
          base_fcf_method, growth_schedule)
        - method="residual_income": financial-company assumptions
          (roe_schedule, payout_ratio, terminal_growth)

    Validates inputs against the matching moat rubric (terminal multiple
    band for DCF, implied justified P/B band for residual income, when a
    score is available) and writes to the IV-Tracker registry with a
    change log. Updates require a reason; every field change is recorded
    with old/new values.
    """
    canonical = normalize_ticker(ticker)

    if method not in REGISTRY_METHODS:
        raise ValidationError(
            f"method must be one of {REGISTRY_METHODS}, got '{method}'"
        )
    if projection_period not in (5, 10):
        raise ValidationError("projection_period must be 5 or 10")
    if not (5.0 <= discount_rate <= 16.0):
        raise ValidationError("discount_rate must be between 5 and 16 percent")

    if method == "dcf":
        if terminal_multiple is None:
            raise ValidationError("terminal_multiple is required for method='dcf'")
        if not (5.0 <= terminal_multiple <= 20.0):
            raise ValidationError("terminal_multiple must be between 5 and 20")
        if base_fcf_method not in (
            "most_recent",
            "average",
            "median",
            "sbc_adjusted",
            "normalized",
        ):
            raise ValidationError(f"Unknown base_fcf_method: {base_fcf_method}")

    if method == "residual_income":
        if payout_ratio is None:
            raise ValidationError(
                "payout_ratio is required for method='residual_income'"
            )
        if not (0.0 <= payout_ratio <= 100.0):
            raise ValidationError(
                f"payout_ratio must be between 0 and 100 percent, "
                f"got {payout_ratio}"
            )
        ri_schedule = [round(float(r), 2) for r in (roe_schedule or [])]
        if len(ri_schedule) != projection_period:
            raise ValidationError(
                f"roe_schedule length ({len(ri_schedule)}) must equal "
                f"projection_period ({projection_period})"
            )
        if terminal_growth is None:
            terminal_growth = 0.0
        if terminal_growth > discount_rate - 2.0:
            raise ValidationError(
                f"terminal_growth ({terminal_growth}) must be at least 2pp "
                f"below discount_rate ({discount_rate})"
            )
        ri_terminal_growth = terminal_growth
    tg_provided = terminal_growth is not None

    if moat_pct is None:
        if method == "residual_income":
            moat_pct = _get_session_financials_moat_pct(canonical)
        else:
            moat_pct = _get_session_moat_pct(canonical)

    # Rubric check: reject out-of-band values on NEW registrations.
    # Updates may move within a tolerance of the existing value with a
    # reason.
    existing = _read_registry_row(canonical)
    if existing is None:
        if method == "dcf":
            rubric = rubric_band(moat_pct)
            lo, hi = rubric["band"]
            if moat_pct is not None and not (lo <= terminal_multiple <= hi):
                raise ValidationError(
                    f"terminal_multiple {terminal_multiple} outside moat rubric band "
                    f"[{lo}, {hi}] for quality '{rubric['label']}'. Re-run with a "
                    "justified multiple inside the band."
                )
        else:
            rubric = financials_rubric_band(moat_pct)
            lo, hi = rubric["band"]
            # Justified P/B on year-1 ROE (current earning power). The
            # faded terminal ROE converges toward the cost of equity and
            # would always imply P/B near 1.0 regardless of quality.
            implied_pb = (ri_schedule[0] - ri_terminal_growth) / (
                discount_rate - ri_terminal_growth
            )
            if implied_pb <= 0:
                raise ValidationError(
                    f"Year-1 ROE ({ri_schedule[0]}%) at or below terminal "
                    f"growth ({ri_terminal_growth}%) implies a non-positive "
                    "justified P/B"
                )
            if moat_pct is not None and not (lo <= implied_pb <= hi):
                raise ValidationError(
                    f"implied justified P/B {implied_pb:.2f}x outside "
                    f"financials rubric band [{lo}, {hi}] for quality "
                    f"'{rubric['label']}'. Adjust the ROE fade or payout."
                )
    else:
        is_default_reason = (
            not reason
            or reason.strip() == ""
            or reason.strip() == "initial registration"
        )
        old_tm = existing.get("terminal_multiple")
        old_dr = existing.get("discount_rate")
        assert old_dr is not None  # NOT NULL in schema
        old_period = existing.get("projection_period")
        old_method = existing.get("method") or "dcf"
        old_payout = existing.get("payout_ratio")
        old_tg = existing.get("terminal_growth")
        substantive_change = (
            method != old_method
            or abs(discount_rate - old_dr) > 0.01
            or projection_period != old_period
            or (
                method == "dcf"
                and terminal_multiple is not None
                and old_tm is not None
                and abs(terminal_multiple - old_tm) > 0.01
            )
            or (
                method == "residual_income"
                and payout_ratio is not None
                and old_payout is not None
                and abs(payout_ratio - old_payout) > 1.0
            )
            or (
                method == "residual_income"
                and tg_provided
                and old_tg is not None
                and abs(ri_terminal_growth - old_tg) > 0.01
            )
        )
        if substantive_change and is_default_reason:
            raise ValidationError(
                "Updating registry methodology requires a substantive reason "
                "citing the changed fundamental (got default/empty reason)"
            )

    schedule = [round(float(g), 2) for g in (growth_schedule or [])]
    if method == "residual_income":
        schedule = ri_schedule
    now = datetime.now().isoformat(timespec="seconds")

    try:
        conn = _connect(readonly=False)
    except sqlite3.Error as e:
        raise ValidationError(f"Cannot open IV-Tracker DB for writing: {e}")

    try:
        _ensure_method_columns(conn)
        if existing is None:
            conn.execute(
                "INSERT INTO assumption_registry (ticker, projection_period, "
                "terminal_multiple, discount_rate, base_fcf_method, "
                "growth_schedule, method, roe_schedule, payout_ratio, "
                "terminal_growth, moat_score, locked_at, change_log) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical,
                    projection_period,
                    terminal_multiple,
                    discount_rate,
                    base_fcf_method if method == "dcf" else None,
                    json.dumps(schedule if method == "dcf" else []),
                    method,
                    json.dumps(schedule if method == "residual_income" else []),
                    payout_ratio if method == "residual_income" else None,
                    ri_terminal_growth if method == "residual_income" else None,
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
            # Omitted optional fields mean "leave unchanged": moat_score
            # None, empty growth_schedule/roe_schedule, and None RI fields
            # never overwrite existing values.
            new_values: Dict[str, Any] = {
                "projection_period": projection_period,
                "discount_rate": discount_rate,
                "method": method,
            }
            if method == "dcf":
                new_values["terminal_multiple"] = terminal_multiple
                new_values["base_fcf_method"] = base_fcf_method
                if growth_schedule:
                    new_values["growth_schedule"] = schedule
            else:
                if roe_schedule:
                    new_values["roe_schedule"] = schedule
                if payout_ratio is not None:
                    new_values["payout_ratio"] = payout_ratio
                if tg_provided:
                    new_values["terminal_growth"] = ri_terminal_growth
            if moat_pct is not None:
                new_values["moat_score"] = moat_pct
            updates: Dict[str, Any] = {}
            for field, new_value in new_values.items():
                old_value = existing.get(field)
                if field in ("growth_schedule", "roe_schedule"):
                    old_cmp = old_value if isinstance(old_value, list) else []
                    if old_cmp != schedule and field in new_values:
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
        "method": method,
        "assumptions": entry,
        "changes": changes,
    }
