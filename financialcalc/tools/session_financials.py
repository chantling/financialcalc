"""Session-based residual-income valuation tools for financial institutions.

Mirrors session_dcf.py: state flows through the session so agents pass
only assumptions (ROE schedule, payout, cost of equity), never raw data.
Designed for banks, insurers, and other Financial Services companies
where FCF DCF is structurally invalid.
"""

import logging
from typing import Any, Dict, List, Optional

from financialcalc.tools.assumption_registry import (
    _read_registry_row,
    normalize_ticker,
)
from financialcalc.tools.data_retrieval import (
    _get_shares_from_balance_sheet,
    _get_yf_ticker,
    get_dividend_history,
    get_yearly_close_history,
)
from financialcalc.tools.financials_valuation import (
    calculate_bvps_series,
    calculate_ddm,
    calculate_justified_pb,
    calculate_residual_income,
    calculate_roe_series,
    classify_valuation_track,
    is_financial_company,
)
from financialcalc.utils.config import settings
from financialcalc.utils.error_handling import ValidationError
from financialcalc.utils.session_manager import session_manager
from financialcalc.utils.validation import (
    assess_valuation,
    calculate_financials_confidence_score,
    validate_financials_against_registry,
    validate_financials_assumptions,
)

logger = logging.getLogger(__name__)


def _get_session_or_error(session_id: str) -> Dict[str, Any]:
    """Get session or raise error if not found.

    Args:
        session_id: Session identifier

    Returns:
        Session data dictionary

    Raises:
        ValidationError: If session not found
    """
    session = session_manager.get_session(session_id)
    if not session:
        raise ValidationError(
            f"No active session found for '{session_id}'. "
            "Call get_financial_data first to create a session."
        )
    return session


def _require_financials_inputs(session: Dict[str, Any]) -> None:
    """Ensure financial data and balance sheet are loaded in the session."""
    if not session.get("financial_data"):
        raise ValidationError(
            "No financial data in session. Call get_financial_data first."
        )
    if not session.get("balance_sheet"):
        raise ValidationError(
            "No balance sheet in session. Call get_balance_sheet first "
            "(equity history is required for residual-income valuation)."
        )


def _fetch_shares_history(symbol: str, years: int) -> List[Optional[float]]:
    """Fetch the historical share-count series from the balance sheet."""
    ticker = _get_yf_ticker(symbol)
    return _get_shares_from_balance_sheet(ticker.balance_sheet, years)


def _align_from_end(
    a: List[Optional[float]], b: List[Optional[float]]
) -> List[Optional[float]]:
    """Align series b to the tail of series a (matching latest years)."""
    n = min(len(a), len(b))
    return b[-n:] if n else []


def _std(values: List[float]) -> Optional[float]:
    """Population standard deviation, or None for insufficient data."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return float(round(variance**0.5, 2))


def _compute_historical_financials(
    session: Dict[str, Any], years: int = 10
) -> Dict[str, Any]:
    """Assemble ROE, BVPS, payout, and P/B history from session data.

    Args:
        session: Session dict with financial_data and balance_sheet
        years: History depth to use

    Returns:
        Dictionary of historical series and summary statistics
    """
    symbol = session.get("symbol", "")
    financial_data = session["financial_data"]
    balance_sheet = session["balance_sheet"]

    net_income = financial_data.get("net_income", [])
    basic_eps = financial_data.get("basic_eps", [])
    total_equity = balance_sheet.get("total_equity", []) or []
    bs_dates = balance_sheet.get("dates", []) or []
    exchange_rate_used = balance_sheet.get("exchange_rate_used")

    roe_result = calculate_roe_series(net_income, total_equity)
    roe_series = roe_result["roe_series"]
    valid_roe = [r for r in roe_series if r is not None]

    gap = session.get("financials_gap_data")
    shares_series: List[Optional[float]] = []
    if gap and gap.get("shares_series"):
        shares_series = gap["shares_series"]
    else:
        try:
            shares_series = _fetch_shares_history(symbol, years)
            if shares_series:
                session_manager.update_session(
                    symbol, {"financials_gap_data": {"shares_series": shares_series}}
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Share history unavailable for {symbol}: {e}")

    bvps_result = calculate_bvps_series(total_equity, shares_series)
    bvps_series = bvps_result["bvps_series"]

    dividend_history = session.get("dividend_history")
    if not dividend_history:
        try:
            dividend_history = get_dividend_history(symbol, years=years)
            session_manager.update_session(
                symbol, {"dividend_history": dividend_history}
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Dividend history unavailable for {symbol}: {e}")
            dividend_history = None

    dps_series: List[Optional[float]] = []
    payout_series: List[Optional[float]] = []
    if dividend_history and dividend_history.get("dividends_per_share"):
        dps_series = dividend_history["dividends_per_share"]
        eps_aligned = _align_from_end(dps_series, basic_eps)
        for dps, eps in zip(dps_series, eps_aligned):
            if dps is not None and eps is not None and eps > 0:
                payout_series.append(round(dps / eps * 100, 1))
            else:
                payout_series.append(None)

    valid_payout = [p for p in payout_series if p is not None]
    avg_payout = (
        round(sum(valid_payout[-5:]) / len(valid_payout[-5:]), 1)
        if valid_payout
        else None
    )

    pb_series: List[Optional[float]] = []
    pb_band: Optional[Dict[str, Any]] = None
    price_history = session.get("price_history")
    if not price_history:
        try:
            price_history = get_yearly_close_history(symbol, years=years)
            session_manager.update_session(symbol, {"price_history": price_history})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Price history unavailable for {symbol}: {e}")
            price_history = None

    if price_history and price_history.get("closes") and exchange_rate_used == 1.0:
        years_list = price_history["years_list"]
        closes = price_history["closes"]
        bvps_by_year = {
            d[:4]: v for d, v in zip(bs_dates, bvps_series) if v is not None
        }
        for year, close in zip(years_list, closes):
            bv = bvps_by_year.get(year)
            if bv is not None and bv > 0:
                pb_series.append(round(close / bv, 2))
            else:
                pb_series.append(None)
        valid_pb = [p for p in pb_series if p is not None]
        if valid_pb:
            sorted_pb = sorted(valid_pb)
            pb_band = {
                "min": sorted_pb[0],
                "max": sorted_pb[-1],
                "median": (
                    sorted_pb[len(sorted_pb) // 2]
                    if len(sorted_pb) % 2 == 1
                    else (
                        sorted_pb[len(sorted_pb) // 2 - 1]
                        + sorted_pb[len(sorted_pb) // 2]
                    )
                    / 2
                ),
                "years": len(valid_pb),
            }

    return {
        "symbol": symbol,
        "dates": bs_dates,
        "roe_series": roe_series,
        "average_roe": roe_result["average_roe"],
        "latest_roe": roe_result["latest_roe"],
        "roe_std": _std(valid_roe),
        "bvps_series": bvps_series,
        "latest_bvps": bvps_result["latest_bvps"],
        "equity_years": len([v for v in total_equity if v is not None]),
        "dps_series": dps_series,
        "dividend_years": len([v for v in dps_series if v is not None]),
        "payout_series": payout_series,
        "average_payout": avg_payout,
        "pb_series": pb_series,
        "pb_band": pb_band,
    }


def _get_coe_baseline(symbol: str) -> Optional[float]:
    """Fetch the CAPM cost of equity via calculate_wacc (best effort)."""
    try:
        from financialcalc.tools.wacc import calculate_wacc

        result = calculate_wacc(symbol)
        return result.get("cost_of_equity")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Cost of equity unavailable for {symbol}: {e}")
        return None


def _resolve_bvps(session: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve current BVPS from latest equity and the best share count."""
    balance_sheet = session["balance_sheet"]
    total_equity = balance_sheet.get("total_equity", []) or []
    valid_equity = [v for v in total_equity if v is not None]
    if not valid_equity:
        raise ValidationError(
            "No equity history available; cannot compute book value per share."
        )
    equity_latest = valid_equity[-1]

    shares = None
    shares_basis = "diluted"
    current_metrics = session.get("current_metrics") or {}
    shares = current_metrics.get("diluted_shares_outstanding")
    if not shares:
        shares = current_metrics.get("shares_outstanding")
        if shares:
            shares_basis = "basic"
    if not shares:
        gap = session.get("financials_gap_data") or {}
        series = gap.get("shares_series") or []
        for v in reversed(series):
            if v is not None and v > 0:
                shares = v
                shares_basis = "balance_sheet"
                break
    if not shares:
        financial_data = session.get("financial_data") or {}
        shares = financial_data.get("diluted_shares_outstanding")
        if shares:
            shares_basis = "diluted"
        else:
            shares = financial_data.get("shares_outstanding")
            if shares:
                shares_basis = "basic"

    if not shares or shares <= 0:
        raise ValidationError(
            "Shares outstanding not available. Call get_current_metrics "
            "before running the financials valuation."
        )

    return {
        "bvps": round(equity_latest / shares, 4),
        "equity_latest": equity_latest,
        "shares": shares,
        "shares_basis": shares_basis,
    }


def get_financials_options(session_id: str) -> Dict[str, Any]:
    """Get inputs and guidance for a residual-income valuation.

    Presents historical ROE, BVPS, payout, and P/B context plus the CAPM
    cost-of-equity anchor so the agent can choose a defensible ROE
    schedule, payout ratio, and discount rate.

    Args:
        session_id: Session identifier (typically ticker symbol)

    Returns:
        Dictionary with historical series, cost_of_equity baseline,
        suggested assumptions, and guidance

    Raises:
        ValidationError: If session, financial data, or balance sheet missing
    """
    session = _get_session_or_error(session_id)
    _require_financials_inputs(session)

    symbol = session.get("symbol", session_id)
    historical = _compute_historical_financials(session)
    session_manager.update_session(session_id, {"financials_data": historical})

    coe_baseline = _get_coe_baseline(symbol)

    current_metrics = session.get("current_metrics") or {}
    current_price = current_metrics.get("current_price")

    bvps_info = None
    try:
        bvps_info = _resolve_bvps(session)
    except ValidationError as e:
        logger.debug(f"BVPS resolution failed: {e}")

    current_pb = None
    if current_price is not None and bvps_info:
        current_pb = round(current_price / bvps_info["bvps"], 2)

    guidance: List[str] = []
    avg_roe = historical["average_roe"]
    if avg_roe is not None:
        suggested_start = round(avg_roe * 0.7, 1)
        guidance.append(
            f"Historical average ROE: {avg_roe:.1f}% (latest "
            f"{historical['latest_roe']:.1f}%, std {historical['roe_std']}pp). "
            f"Start the schedule near {suggested_start}% (a 30% conservative "
            "reduction) unless fundamentals justify otherwise."
        )
    else:
        guidance.append(
            "No computable ROE history; anchor the schedule on the company's "
            "reported ROE and sector norms, and document the basis."
        )
    if coe_baseline is not None:
        guidance.append(
            f"CAPM cost of equity: {coe_baseline}%. Use it as the discount "
            "rate; the final-year ROE must fade to within 2pp of it."
        )
    else:
        guidance.append(
            "No CAPM baseline available; choose a cost of equity inside the "
            "8-12% band for a first analysis."
        )
    if historical["average_payout"] is not None:
        guidance.append(
            f"Historical average payout: {historical['average_payout']:.1f}%. "
            "Reuse it unless capital allocation has changed."
        )
    else:
        guidance.append(
            "No payout history available; supply payout_ratio explicitly "
            "based on the company's capital-allocation policy."
        )
    if historical["pb_band"]:
        guidance.append(
            f"Historical P/B band: {historical['pb_band']['min']}-"
            f"{historical['pb_band']['max']}x (median "
            f"{historical['pb_band']['median']}x). Use as context for the "
            "implied justified P/B."
        )
    if historical["roe_std"] is not None and historical["roe_std"] > 5:
        guidance.append(
            "ROE history is volatile (>5pp std); favor the low end of the "
            "schedule and a shorter projection period."
        )
    classification = classify_valuation_track(
        session.get("financial_data"),
        session.get("balance_sheet"),
        session.get("current_metrics"),
    )
    if not classification["track"] == "financial":
        if classification["asset_light_override"]:
            guidance.append(
                "Asset-light override: provider sector is Financial Services "
                f"but {classification['reason']} Do NOT run a residual-income "
                "valuation here — use run_dcf_analysis (operating track)."
            )
        else:
            guidance.append(
                "Note: this company is not classified in a financial sector; "
                "FCF DCF (run_dcf_analysis) is the standard method here."
            )

    result = {
        "session_id": session_id,
        "symbol": symbol,
        "is_financial_company": is_financial_company(
            session.get("financial_data") or {}
        ),
        "valuation_track": classification,
        "historical": historical,
        "cost_of_equity_baseline": coe_baseline,
        "current_bvps": bvps_info["bvps"] if bvps_info else None,
        "shares_basis": bvps_info["shares_basis"] if bvps_info else None,
        "current_price": current_price,
        "current_pb": current_pb,
        "guidance": guidance,
    }

    session_manager.update_session(session_id, {"financials_options": result})
    return result


def run_financials_valuation(
    session_id: str,
    roe_schedule: List[float],
    cost_of_equity: Optional[float] = None,
    payout_ratio: Optional[float] = None,
    terminal_growth: float = 0.0,
    margin_of_safety: float = 30.0,
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a residual-income valuation using data from the session.

    The primary valuation for financial companies (banks, insurers).
    Intrinsic value = book value + PV of residual income (earnings above
    the cost of equity), with justified-P/B and DDM cross-checks computed
    from the same assumptions. Assumptions are hard-validated against the
    residual-income registry (when registered) or absolute first-run
    guardrails; deviations require override_reason.

    Args:
        session_id: Session identifier (typically ticker symbol)
        roe_schedule: ROE assumption per projection year (percent);
            must fade toward the cost of equity
        cost_of_equity: Cost of equity (percent); defaults to the CAPM
            baseline when omitted
        payout_ratio: Payout ratio (percent, 0-100); defaults to the
            historical average when omitted
        terminal_growth: Continuing residual-income growth (percent,
            default 0); must be >= 2pp below the cost of equity
        margin_of_safety: Margin of safety percentage (default: 30)
        override_reason: Required ONLY when assumptions deviate from the
            registry; must cite the changed fundamental

    Returns:
        Complete residual-income valuation with cross-checks, confidence
        score, valuation assessment, and sensitivity matrix

    Raises:
        ValidationError: If required data is missing or guardrails are
            violated without an override_reason
    """
    session = _get_session_or_error(session_id)
    _require_financials_inputs(session)

    # Asset-light companies classified Financial Services by the provider
    # (payment networks) cannot be valued on book value — their equity is a
    # capital-return residual. Hard-block unless the agent explicitly
    # overrides.
    classification = classify_valuation_track(
        session.get("financial_data"),
        session.get("balance_sheet"),
        session.get("current_metrics"),
    )
    if classification["asset_light_override"] and not (
        override_reason and override_reason.strip()
    ):
        raise ValidationError(
            "Residual income on book value is structurally inappropriate "
            f"here: {classification['reason']} Use run_dcf_analysis "
            "(operating track) instead, or pass override_reason citing why "
            "book value remains economically meaningful for this company."
        )

    symbol = session.get("symbol", session_id)
    historical = session.get("financials_data") or _compute_historical_financials(
        session
    )

    if not roe_schedule:
        raise ValidationError("roe_schedule cannot be empty")

    coe_baseline = _get_coe_baseline(symbol)
    if cost_of_equity is None:
        if coe_baseline is None:
            raise ValidationError(
                "cost_of_equity required: no CAPM baseline available "
                "(calculate_wacc failed)."
            )
        cost_of_equity = coe_baseline

    if payout_ratio is None:
        payout_ratio = historical.get("average_payout")
        if payout_ratio is None:
            raise ValidationError(
                "payout_ratio required: no dividend history available to "
                "infer the historical average payout."
            )

    # Soft warnings
    validation_warnings = validate_financials_assumptions(
        roe_schedule,
        cost_of_equity,
        payout_ratio,
        terminal_growth=terminal_growth,
        historical_roe=historical.get("average_roe"),
        historical_payout=historical.get("average_payout"),
    )

    # Registry guardrails (hard reject unless override_reason supplied)
    canonical = normalize_ticker(symbol)
    registry_entry = _read_registry_row(canonical)
    registry_note = None
    if registry_entry is not None and registry_entry.get("method") not in (
        None,
        "residual_income",
    ):
        registry_note = (
            f"Registry holds a '{registry_entry.get('method')}' methodology "
            "for this ticker; first-run guardrails apply to this "
            "residual-income analysis."
        )
        ri_registry: Optional[Dict[str, Any]] = None
    else:
        ri_registry = registry_entry

    violations = validate_financials_against_registry(
        roe_schedule,
        cost_of_equity,
        payout_ratio,
        terminal_growth,
        registry=ri_registry,
        coe_baseline=coe_baseline,
    )
    overridden = bool(violations and override_reason and override_reason.strip())
    registry_match = {
        "ticker": canonical,
        "registered": ri_registry is not None,
        "method": "residual_income",
        "violations": violations,
        "overridden": overridden,
        "override_reason": override_reason if overridden else None,
    }
    if violations and not overridden:
        raise ValidationError(
            "Assumption guardrails violated: "
            + "; ".join(violations)
            + ". Re-run with assumptions matching the registry (see "
            "get_prior_assumptions), or pass override_reason citing the "
            "changed fundamental. After an accepted override, call "
            "register_assumptions with method='residual_income' to update "
            "the registry."
        )

    bvps_info = _resolve_bvps(session)
    bvps = bvps_info["bvps"]

    # Primary valuation: residual income
    ri_result = calculate_residual_income(
        bvps,
        roe_schedule,
        cost_of_equity,
        payout_ratio,
        terminal_growth,
    )
    intrinsic_value = ri_result["intrinsic_value"]
    buy_price = intrinsic_value * (1 - margin_of_safety / 100)

    # Cross-check 1: justified P/B on terminal steady-state values
    jpb_result = calculate_justified_pb(
        roe=roe_schedule[-1],
        cost_of_equity=cost_of_equity,
        growth=terminal_growth,
        book_value_per_share=bvps,
    )

    # Cross-check 2: DDM on the same projection's dividend stream
    ddm_result = None
    if payout_ratio > 0:
        try:
            ddm_result = calculate_ddm(
                ri_result["dividends_per_share"],
                cost_of_equity,
                terminal_growth,
            )
        except ValidationError as e:
            logger.debug(f"DDM cross-check skipped: {e}")

    current_metrics = session.get("current_metrics") or {}
    current_price = current_metrics.get("current_price")
    current_pb = round(current_price / bvps, 2) if current_price is not None else None

    if current_pb is not None and current_pb >= settings.asset_light_pb_min:
        validation_warnings.append(
            f"Current P/B of {current_pb}x is far above the level where book "
            "value can anchor intrinsic value; confirm this is a genuine "
            "balance-sheet institution (bank/insurer) before relying on "
            "this residual-income result."
        )

    confidence = calculate_financials_confidence_score(
        roe_schedule,
        cost_of_equity,
        payout_ratio,
        historical_roe=historical.get("average_roe"),
        historical_roe_std=historical.get("roe_std"),
        coe_baseline=coe_baseline,
        equity_years=historical.get("equity_years"),
        dividend_years=historical.get("dividend_years"),
    )

    valuation = assess_valuation(current_price, intrinsic_value)

    # Sensitivity: flat ROE shift x cost-of-equity grid
    roe_shifts = [-2.0, 0.0, 2.0]
    coe_values = [cost_of_equity - 1.0, cost_of_equity, cost_of_equity + 1.0]
    matrix: List[List[Optional[float]]] = []
    for shift in roe_shifts:
        row: List[Optional[float]] = []
        for coe in coe_values:
            try:
                shifted = [r + shift for r in roe_schedule]
                res = calculate_residual_income(
                    bvps, shifted, coe, payout_ratio, terminal_growth
                )
                row.append(res["intrinsic_value"])
            except ValidationError:
                row.append(None)
        matrix.append(row)
    sensitivity = {
        "variables": ["roe_shift", "cost_of_equity"],
        "roe_shifts": roe_shifts,
        "cost_of_equity_values": [round(c, 2) for c in coe_values],
        "matrix": matrix,
    }

    results = {
        "symbol": symbol,
        "method": "residual_income",
        "bvps": bvps,
        "equity_latest": bvps_info["equity_latest"],
        "shares_outstanding": bvps_info["shares"],
        "shares_basis": bvps_info["shares_basis"],
        "roe_schedule": roe_schedule,
        "cost_of_equity": cost_of_equity,
        "payout_ratio": payout_ratio,
        "terminal_growth": terminal_growth,
        "margin_of_safety": margin_of_safety,
        "projection_years": len(roe_schedule),
        "projection_table": ri_result["projection_table"],
        "pv_residual_income_sum": ri_result["pv_residual_income_sum"],
        "terminal_value": ri_result["terminal_value"],
        "pv_terminal_value": ri_result["pv_terminal_value"],
        "terminal_bvps": ri_result["terminal_bvps"],
        "value_composition": ri_result["value_composition"],
        "intrinsic_value": intrinsic_value,
        "buy_price": round(buy_price, 2),
        "current_price": current_price,
        "current_pb": current_pb,
        "justified_pb_cross_check": {
            "justified_pb": jpb_result["justified_pb"],
            "implied_value": jpb_result.get("intrinsic_value"),
            "basis": "terminal-year ROE and terminal growth",
        },
        "ddm_cross_check": (
            {
                "intrinsic_value": ddm_result["intrinsic_value"],
                "pv_dividends_sum": ddm_result["pv_dividends_sum"],
                "terminal_value": ddm_result["terminal_value"],
            }
            if ddm_result
            else None
        ),
        "coe_baseline": coe_baseline,
        "confidence": confidence,
        "valuation": valuation,
        "validation_warnings": validation_warnings,
        "registry_match": registry_match,
        "valuation_track": classification,
        "sensitivity_analysis": sensitivity,
        "calculation_log": [
            f"Registry: {'matched' if registry_match['registered'] and not violations else 'n/a'}"
            + (f" (OVERRIDDEN: {override_reason})" if overridden else ""),
            f"Book value per share: ${bvps:.2f} "
            f"({bvps_info['shares_basis']} shares)",
            f"ROE schedule: {roe_schedule}",
            f"Cost of equity: {cost_of_equity}% (baseline {coe_baseline}%)",
            f"Payout ratio: {payout_ratio}%",
            f"PV of residual income: ${ri_result['pv_residual_income_sum']:.2f}",
            f"PV of terminal value: ${ri_result['pv_terminal_value']:.2f}",
            f"Residual-income IV: ${intrinsic_value:.2f}",
            f"Justified P/B cross-check: {jpb_result['justified_pb']}x "
            f"(implied ${jpb_result.get('intrinsic_value')})",
            (
                f"DDM cross-check IV: ${ddm_result['intrinsic_value']}"
                if ddm_result
                else "DDM cross-check: skipped (payout is zero)"
            ),
            f"Buy price ({margin_of_safety}% MoS): ${buy_price:.2f}",
        ],
    }
    if registry_note:
        results["registry_note"] = registry_note

    session_manager.update_session(session_id, {"financials_results": results})
    return results
