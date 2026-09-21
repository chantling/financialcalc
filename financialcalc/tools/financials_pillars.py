"""8-Pillar analysis variant for financial institutions.

Banks and insurers are scored on equity-based pillars (ROE, BVPS growth,
payout sustainability, P/B vs justified P/B) instead of the FCF/ROIC
pillars that are structurally meaningless for them. Output shape mirrors
run_eight_pillar_analysis so downstream consumers (registry rubric,
reports) treat them identically.
"""

import logging
from typing import Any, Dict, List, Optional

from financialcalc.tools.eight_pillar import (
    _calculate_pe_ratio,
    _compute_cagr_pct,
    _filter_valid,
    _get_session_data,
    _pillar_pass_fail,
    _safe_avg,
)
from financialcalc.tools.financials_valuation import (
    calculate_justified_pb,
)
from financialcalc.tools.session_financials import (
    _compute_historical_financials,
    _get_coe_baseline,
    _resolve_bvps,
)
from financialcalc.utils.config import settings
from financialcalc.utils.error_handling import ValidationError
from financialcalc.utils.session_manager import session_manager

logger = logging.getLogger(__name__)


def _calculate_roe_pillar(
    roe_series: List[Optional[float]], roe_min: float
) -> Dict[str, Any]:
    """Pillar 2: average ROE above threshold."""
    avg_roe = _safe_avg(roe_series)
    if avg_roe is None:
        return _pillar_pass_fail(
            "Return on Equity (avg)",
            None,
            roe_min,
            None,
            "value > threshold",
            "ROE history not available",
            {"roe_series": roe_series},
        )
    passed = avg_roe > roe_min
    valid = _filter_valid(roe_series)
    log = f"avg ROE ({avg_roe:.1f}%) over {len(valid)} years vs {roe_min}% threshold"
    return _pillar_pass_fail(
        "Return on Equity (avg)",
        avg_roe,
        roe_min,
        passed,
        "value > threshold",
        log,
        {"roe_series": roe_series, "average_roe": avg_roe},
    )


def _calculate_bvps_growth_pillar(
    bvps_series: List[Optional[float]],
) -> Dict[str, Any]:
    """Pillar 3: BVPS compounding (the value engine for financials)."""
    cagr = _compute_cagr_pct(bvps_series)
    if cagr is None:
        return _pillar_pass_fail(
            "Book Value Per Share Growth",
            None,
            0.0,
            None,
            "CAGR > 0",
            "BVPS history not available",
            {"bvps_series": bvps_series},
        )
    passed = cagr > 0
    valid = _filter_valid(bvps_series)
    log = (
        f"CAGR(BVPS) = ({valid[-1]:.2f} / {valid[0]:.2f})^"
        f"(1/{len(valid) - 1}) - 1 = {cagr:.1f}%"
    )
    return _pillar_pass_fail(
        "Book Value Per Share Growth",
        cagr,
        0.0,
        passed,
        "CAGR > 0",
        log,
        {"bvps_series": bvps_series},
    )


def _calculate_payout_pillar(
    payout_series: List[Optional[float]], payout_max: float
) -> Dict[str, Any]:
    """Pillar 7: payout sustainability (dividends covered by earnings)."""
    valid = _filter_valid(payout_series)
    if not valid:
        return _pillar_pass_fail(
            "Payout Sustainability",
            None,
            payout_max,
            None,
            "0 < value <= threshold",
            "No dividend history available (payout cannot be assessed)",
            {"payout_series": payout_series},
        )
    avg_payout = sum(valid) / len(valid)
    passed = 0 < avg_payout <= payout_max
    log = (
        f"avg payout {avg_payout:.1f}% over {len(valid)} years vs "
        f"{payout_max}% ceiling"
    )
    return _pillar_pass_fail(
        "Payout Sustainability",
        avg_payout,
        payout_max,
        passed,
        "0 < value <= threshold",
        log,
        {"payout_series": payout_series, "average_payout": avg_payout},
    )


def _calculate_pb_vs_justified_pillar(
    current_pb: Optional[float],
    average_roe: Optional[float],
    coe: Optional[float],
    payout_ratio: Optional[float],
) -> Dict[str, Any]:
    """Pillar 8: current P/B below justified P/B.

    Justified P/B = (ROE - g) / (r - g) with g = sustainable growth
    ROE x (1 - payout), clamped to at most r - 2pp.
    """
    if current_pb is None or average_roe is None or coe is None:
        return _pillar_pass_fail(
            "P/B vs Justified P/B",
            None,
            0.0,
            None,
            "current < justified",
            "Current P/B, ROE history, or cost of equity not available",
            {"current_pb": current_pb, "average_roe": average_roe, "coe": coe},
        )

    sustainable_g = average_roe * (
        1 - (payout_ratio if payout_ratio is not None else 0.0) / 100
    )
    max_g = coe - 2.0
    growth = min(sustainable_g, max_g)

    try:
        jpb = calculate_justified_pb(roe=average_roe, cost_of_equity=coe, growth=growth)
    except ValidationError as e:
        return _pillar_pass_fail(
            "P/B vs Justified P/B",
            None,
            0.0,
            None,
            "current < justified",
            f"Justified P/B not computable: {e}",
            {"average_roe": average_roe, "coe": coe, "growth": growth},
        )

    justified_pb = jpb["justified_pb"]
    passed = current_pb < justified_pb
    log = (
        f"justified P/B = ({average_roe:.1f} - {growth:.1f}) / "
        f"({coe} - {growth:.1f}) = {justified_pb:.2f}x; current {current_pb}x"
    )
    return _pillar_pass_fail(
        "P/B vs Justified P/B",
        current_pb,
        round(justified_pb, 2),
        passed,
        "current < justified",
        log,
        {
            "current_pb": current_pb,
            "justified_pb": justified_pb,
            "average_roe": average_roe,
            "cost_of_equity": coe,
            "sustainable_growth": round(growth, 2),
        },
    )


def run_financials_pillar_analysis(
    symbol: str,
    years: int = 5,
    force_refresh: bool = False,
    clear_cache: bool = False,
) -> Dict[str, Any]:
    """Run the financial-institution variant of the 8-pillar analysis.

    Replaces the FCF/ROIC pillars with equity-based ones:
    1. PE Ratio (5-year), 2. ROE, 3. BVPS Growth, 4. Revenue Growth,
    5. Net Income Growth, 6. Shares Trend, 7. Payout Sustainability,
    8. P/B vs Justified P/B.

    Args:
        symbol: Stock ticker symbol (e.g., "PGR")
        years: Number of years of historical data, 1-10 (default: 5)
        force_refresh: Force fresh data fetch from Yahoo Finance
        clear_cache: Clear cache before fetching

    Returns:
        Dictionary with symbol, years_analyzed, score
        ({passed, total, evaluated, skipped, percentage}),
        thresholds_used, pillars, and data_sources

    Raises:
        ValidationError: If symbol is invalid or years out of range
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")
    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    data = _get_session_data(symbol, years, force_refresh, clear_cache)

    financial_data = data["financial_data"]
    balance_sheet = data["balance_sheet"]
    current_metrics = data["current_metrics"]
    gap_data = data.get("gap_data") or {}

    market_cap = current_metrics.get("market_cap") if current_metrics else None
    current_price = current_metrics.get("current_price") if current_metrics else None

    revenue = financial_data.get("revenue", [])
    net_income = financial_data.get("net_income", [])

    session = session_manager.get_session(symbol) or {}
    historical = session.get("financials_data") or _compute_historical_financials(
        (
            session
            if session
            else {
                "symbol": symbol,
                "financial_data": financial_data,
                "balance_sheet": balance_sheet,
            }
        ),
        years=10,
    )
    session_manager.update_session(symbol, {"financials_data": historical})

    roe_series = historical.get("roe_series", [])
    bvps_series = historical.get("bvps_series", [])
    payout_series = historical.get("payout_series", [])

    shares_arr = gap_data.get("basic_average_shares", [])

    coe = _get_coe_baseline(symbol)
    current_pb = None
    try:
        bvps_info = _resolve_bvps(
            {
                "balance_sheet": balance_sheet,
                "current_metrics": current_metrics,
                "financial_data": financial_data,
                "financials_gap_data": session.get("financials_gap_data"),
            }
        )
        if current_price is not None:
            current_pb = round(current_price / bvps_info["bvps"], 2)
    except ValidationError as e:
        logger.debug(f"BVPS resolution failed for {symbol}: {e}")

    thresholds = {
        "pe_max": settings.eight_pillar_pe_max,
        "roe_min": settings.financials_roe_min,
        "payout_max": settings.financials_payout_max,
    }

    pillars: Dict[str, Dict[str, Any]] = {}
    passed_count = 0
    total_count = 8
    skipped_count = 0

    def _record(key: str, result: Dict[str, Any]) -> None:
        nonlocal passed_count, skipped_count
        pillars[key] = result
        if result["passed"] is None:
            skipped_count += 1
        elif result["passed"]:
            passed_count += 1

    _record(
        "pe_ratio",
        _calculate_pe_ratio(market_cap, net_income, thresholds["pe_max"]),
    )
    _record("roe", _calculate_roe_pillar(roe_series, thresholds["roe_min"]))
    _record("bvps_growth", _calculate_bvps_growth_pillar(bvps_series))
    _record(
        "revenue_growth",
        _growth_pillar("Revenue Growth", revenue),
    )
    _record(
        "net_income_growth",
        _growth_pillar("Net Income Growth", net_income),
    )
    _record("shares_trend", _shares_trend_pillar(shares_arr))
    _record(
        "payout_sustainability",
        _calculate_payout_pillar(payout_series, thresholds["payout_max"]),
    )
    _record(
        "pb_vs_justified",
        _calculate_pb_vs_justified_pillar(
            current_pb,
            historical.get("average_roe"),
            coe,
            historical.get("average_payout"),
        ),
    )

    evaluated = total_count - skipped_count
    percentage = round((passed_count / evaluated) * 100, 1) if evaluated > 0 else 0.0

    score = {
        "passed": passed_count,
        "total": total_count,
        "evaluated": evaluated,
        "skipped": skipped_count,
        "percentage": percentage,
    }

    result = {
        "symbol": symbol,
        "years_analyzed": years,
        "method": "financials",
        "score": score,
        "thresholds_used": thresholds,
        "pillars": pillars,
        "cost_of_equity_baseline": coe,
        "current_pb": current_pb,
        "data_sources": {
            "financial_data_cached": financial_data.get("cached", False),
            "balance_sheet_cached": (
                balance_sheet.get("cached", False) if balance_sheet else False
            ),
            "current_metrics_cached": (
                current_metrics.get("cached", False) if current_metrics else False
            ),
            "gap_data_from_session": data.get("gap_data") is not None,
        },
    }

    session_manager.update_session(symbol, {"financials_pillar_analysis": result})
    return result


def _growth_pillar(name: str, series: List[Optional[float]]) -> Dict[str, Any]:
    """Generic CAGR > 0 pillar."""
    cagr = _compute_cagr_pct(series)
    if cagr is None:
        return _pillar_pass_fail(
            name,
            None,
            0.0,
            None,
            "CAGR > 0",
            f"{name} history not available",
            {"series": series},
        )
    passed = cagr > 0
    valid = _filter_valid(series)
    log = f"CAGR({name}) from {len(valid)} years = {cagr:.1f}%"
    return _pillar_pass_fail(
        name, cagr, 0.0, passed, "CAGR > 0", log, {"series": series}
    )


def _shares_trend_pillar(
    shares_arr: List[Optional[float]],
) -> Dict[str, Any]:
    """Pillar 6: share count decreasing (buybacks) vs increasing."""
    valid = _filter_valid(shares_arr)
    if len(valid) < 2:
        return _pillar_pass_fail(
            "Shares Outstanding Trend",
            None,
            0.0,
            None,
            "decreasing",
            "Share history not available",
            {"shares_series": shares_arr},
        )
    first, last = valid[0], valid[-1]
    passed = last < first
    pct = (last / first - 1) * 100 if first else None
    direction = "decreasing (buybacks)" if passed else "increasing (dilution)"
    log = f"shares {first:,.0f} -> {last:,.0f} ({pct:+.1f}%, {direction})"
    return _pillar_pass_fail(
        "Shares Outstanding Trend",
        round(pct, 2) if pct is not None else None,
        0.0,
        passed,
        "decreasing",
        log,
        {"shares_series": shares_arr},
    )
