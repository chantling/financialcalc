"""8-Pillar Stock Analysis tool for FinancialCalc MCP Server.

Evaluates a stock across 8 fundamental pillars with fixed thresholds
(configurable via .env). All data is fetched internally from Yahoo Finance,
reusing cached session data where available.
"""

import logging
from typing import Any, Dict, List, Optional

from financialcalc.tools.core_calculations import calculate_cagr
from financialcalc.tools.data_retrieval import (
    _extract_series,
    _get_dates,
    _get_exchange_rate_to_usd,
    _get_yf_ticker,
)
from financialcalc.utils.config import settings
from financialcalc.utils.error_handling import ValidationError
from financialcalc.utils.session_manager import session_manager

logger = logging.getLogger(__name__)


def _get_session_data(
    symbol: str, years: int, force_refresh: bool, clear_cache: bool
) -> Dict[str, Any]:
    """Retrieve all needed data, reusing session cache where available.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of historical data
        force_refresh: Force fresh data fetch
        clear_cache: Clear cache before fetching

    Returns:
        Dictionary with financial_data, balance_sheet, current_metrics,
        and gap_data (EBIT, tax expense, historical shares).
    """
    from financialcalc.tools.data_retrieval import (get_balance_sheet,
                                                    get_current_metrics,
                                                    get_financial_data)

    session = session_manager.get_session(symbol) or {}

    financial_data = session.get("financial_data")
    if not financial_data or force_refresh:
        financial_data = get_financial_data(
            symbol, years=years, force_refresh=force_refresh, clear_cache=clear_cache
        )

    balance_sheet = session.get("balance_sheet")
    if not balance_sheet or force_refresh:
        balance_sheet = get_balance_sheet(
            symbol, years=years, force_refresh=force_refresh, clear_cache=clear_cache
        )

    current_metrics = session.get("current_metrics")
    if not current_metrics or force_refresh:
        current_metrics = get_current_metrics(
            symbol, force_refresh=force_refresh, clear_cache=clear_cache
        )

    gap_data = session.get("eight_pillar_gap_data")
    if not gap_data or force_refresh:
        gap_data = _fetch_gap_data(symbol, years)
        if gap_data:
            session_manager.update_session(symbol, {"eight_pillar_gap_data": gap_data})

    return {
        "financial_data": financial_data,
        "balance_sheet": balance_sheet,
        "current_metrics": current_metrics,
        "gap_data": gap_data,
    }


def _fetch_gap_data(symbol: str, years: int) -> Optional[Dict[str, Any]]:
    """Fetch data not available from existing tools: EBIT, tax expense,
    and historical Basic Average Shares.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of data

    Returns:
        Dictionary with ebit, income_tax_expense, basic_average_shares arrays
        and dates, or None on failure.
    """
    try:
        ticker = _get_yf_ticker(symbol)
        income_stmt = ticker.income_stmt

        ebit = _extract_series(income_stmt, "EBIT", years)
        if not ebit:
            ebit = _extract_series(income_stmt, "Operating Income", years)

        tax_expense = _extract_series(income_stmt, "Tax Provision", years)
        if not tax_expense:
            tax_expense = _extract_series(income_stmt, "Income Tax Expense", years)

        basic_shares = _extract_series(income_stmt, "Basic Average Shares", years)
        if not basic_shares:
            basic_shares = _extract_series(
                income_stmt, "Weighted Average Shares", years
            )

        pre_tax_income = _extract_series(income_stmt, "Pretax Income", years)
        if not pre_tax_income:
            pre_tax_income = _extract_series(income_stmt, "Income Before Tax", years)

        financial_currency = ticker.info.get("financialCurrency")
        exchange_rate = _get_exchange_rate_to_usd(financial_currency)

        if exchange_rate is not None and exchange_rate != 1.0:
            from financialcalc.tools.data_retrieval import \
                _convert_array_to_usd

            ebit = _convert_array_to_usd(ebit, exchange_rate)
            tax_expense = _convert_array_to_usd(tax_expense, exchange_rate)
            pre_tax_income = _convert_array_to_usd(pre_tax_income, exchange_rate)

        dates = _get_dates(income_stmt, years)

        return {
            "ebit": ebit,
            "income_tax_expense": tax_expense,
            "pre_tax_income": pre_tax_income,
            "basic_average_shares": basic_shares,
            "dates": dates,
        }
    except Exception as e:
        logger.warning(f"Could not fetch gap data for {symbol}: {e}")
        return None


def _filter_valid(values: List[Optional[float]]) -> List[float]:
    """Filter None values from a list."""
    return [v for v in values if v is not None]


def _safe_avg(values: List[Optional[float]]) -> Optional[float]:
    """Calculate average of non-None values."""
    valid = _filter_valid(values)
    if not valid:
        return None
    return sum(valid) / len(valid)


def _compute_cagr_pct(values: List[Optional[float]]) -> Optional[float]:
    """Compute CAGR as a percentage, returning None if insufficient data."""
    valid = _filter_valid(values)
    if len(valid) < 2:
        return None
    try:
        padded: List[Optional[float]] = [v for v in valid]
        result = calculate_cagr(padded)
        return result.get("cagr")
    except Exception:
        return None


def _pillar_pass_fail(
    pillar_name: str,
    value: Optional[float],
    threshold: float,
    passed: Optional[bool],
    comparison: str,
    calculation_log: str,
    details: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a standardized pillar result dict.

    Args:
        pillar_name: Human-readable pillar name
        value: Computed metric value
        threshold: Threshold for pass/fail
        passed: True/False/None (None = skipped due to missing data)
        comparison: Description of comparison (e.g., "value < threshold")
        calculation_log: Human-readable description of the calculation
        details: Raw data used in calculation

    Returns:
        Standardized pillar result dictionary
    """
    return {
        "pillar": pillar_name,
        "value": round(value, 2) if value is not None else None,
        "threshold": threshold,
        "passed": passed,
        "comparison": comparison,
        "calculation_log": calculation_log,
        "details": details,
    }


def run_eight_pillar_analysis(
    symbol: str,
    years: int = 5,
    force_refresh: bool = False,
    clear_cache: bool = False,
) -> Dict[str, Any]:
    """Run complete 8-pillar stock analysis.

    Fetches all required data internally from Yahoo Finance, reusing cached
    session data where available. The agent only needs to supply the ticker
    symbol and optionally the number of years.

    Pillar thresholds are configurable via .env:
        EIGHT_PILLAR_PE_MAX (default: 22.5)
        EIGHT_PILLAR_ROIC_MIN (default: 9.0)
        EIGHT_PILLAR_DEBT_TO_FCF_MAX (default: 5.0)
        EIGHT_PILLAR_FCF_MULTIPLE (default: 22.5)

    Args:
        symbol: Stock ticker symbol (e.g., "AAPL")
        years: Number of years of historical data, 1-10 (default: 5)
        force_refresh: Force fresh data fetch from Yahoo Finance
        clear_cache: Clear cache before fetching

    Returns:
        Dictionary with:
            - symbol: Ticker symbol
            - years_analyzed: Number of years used
            - score: {passed, total, percentage, skipped}
            - thresholds_used: Current threshold values
            - pillars: Dict of all 8 pillar results
            - data_sources: Cache status of data used

    Raises:
        ValidationError: If symbol is invalid or years out of range
        CalculationError: If critical data cannot be retrieved
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

    revenue = financial_data.get("revenue", [])
    net_income = financial_data.get("net_income", [])
    fcf = financial_data.get("free_cash_flow", [])

    total_debt_arr = balance_sheet.get("total_debt", []) if balance_sheet else []
    total_equity_arr = balance_sheet.get("total_equity", []) if balance_sheet else []
    total_cash_arr = balance_sheet.get("total_cash", []) if balance_sheet else []
    lt_debt_arr = balance_sheet.get("long_term_debt", []) if balance_sheet else []

    ebit_arr = gap_data.get("ebit", [])
    tax_arr = gap_data.get("income_tax_expense", [])
    pre_tax_arr = gap_data.get("pre_tax_income", [])
    shares_arr = gap_data.get("basic_average_shares", [])

    thresholds = {
        "pe_max": settings.eight_pillar_pe_max,
        "roic_min": settings.eight_pillar_roic_min,
        "debt_to_fcf_max": settings.eight_pillar_debt_to_fcf_max,
        "fcf_multiple": settings.eight_pillar_fcf_multiple,
    }

    pillars: Dict[str, Dict[str, Any]] = {}
    passed_count = 0
    total_count = 8
    skipped_count = 0

    # --- Pillar 1: PE Ratio (5-year) ---
    pe_result = _calculate_pe_ratio(market_cap, net_income, thresholds["pe_max"])
    pillars["pe_ratio"] = pe_result
    if pe_result["passed"] is None:
        skipped_count += 1
    elif pe_result["passed"]:
        passed_count += 1

    # --- Pillar 2: ROIC ---
    roic_result = _calculate_roic(
        ebit_arr,
        tax_arr,
        pre_tax_arr,
        total_debt_arr,
        total_equity_arr,
        total_cash_arr,
        thresholds["roic_min"],
    )
    pillars["roic"] = roic_result
    if roic_result["passed"] is None:
        skipped_count += 1
    elif roic_result["passed"]:
        passed_count += 1

    # --- Pillar 3: Revenue Growth ---
    rev_growth = _calculate_growth_pillar(
        "Revenue Growth",
        revenue,
        "CAGR(revenue) > 0",
    )
    pillars["revenue_growth"] = rev_growth
    if rev_growth["passed"] is None:
        skipped_count += 1
    elif rev_growth["passed"]:
        passed_count += 1

    # --- Pillar 4: Net Income Growth ---
    ni_growth = _calculate_growth_pillar(
        "Net Income Growth",
        net_income,
        "CAGR(net_income) > 0",
    )
    pillars["net_income_growth"] = ni_growth
    if ni_growth["passed"] is None:
        skipped_count += 1
    elif ni_growth["passed"]:
        passed_count += 1

    # --- Pillar 5: Shares Outstanding Trend ---
    shares_result = _calculate_shares_trend(shares_arr)
    pillars["shares_trend"] = shares_result
    if shares_result["passed"] is None:
        skipped_count += 1
    elif shares_result["passed"]:
        passed_count += 1

    # --- Pillar 6: Long-Term Liabilities ---
    debt_result = _calculate_debt_to_fcf(
        lt_debt_arr, fcf, thresholds["debt_to_fcf_max"]
    )
    pillars["long_term_liabilities"] = debt_result
    if debt_result["passed"] is None:
        skipped_count += 1
    elif debt_result["passed"]:
        passed_count += 1

    # --- Pillar 7: FCF Growth ---
    fcf_growth = _calculate_growth_pillar(
        "Free Cash Flow Growth",
        fcf,
        "CAGR(fcf) > 0",
    )
    pillars["fcf_growth"] = fcf_growth
    if fcf_growth["passed"] is None:
        skipped_count += 1
    elif fcf_growth["passed"]:
        passed_count += 1

    # --- Pillar 8: FCF Multiple ---
    fcf_mult = _calculate_fcf_multiple(
        market_cap,
        fcf,
        thresholds["fcf_multiple"],
    )
    pillars["fcf_multiple"] = fcf_mult
    if fcf_mult["passed"] is None:
        skipped_count += 1
    elif fcf_mult["passed"]:
        passed_count += 1

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
        "score": score,
        "thresholds_used": thresholds,
        "pillars": pillars,
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

    session_manager.update_session(symbol, {"eight_pillar_analysis": result})

    return result


def _calculate_pe_ratio(
    market_cap: Optional[float],
    net_income: List[Optional[float]],
    pe_max: float,
) -> Dict[str, Any]:
    """Pillar 1: 5-year PE Ratio = market_cap / avg(5yr net_income)."""
    avg_ni = _safe_avg(net_income)

    if market_cap is None or avg_ni is None:
        return _pillar_pass_fail(
            "PE Ratio (5-Year)",
            None,
            pe_max,
            None,
            "value < threshold",
            "Insufficient data",
            {"market_cap": market_cap, "avg_net_income": avg_ni},
        )

    if avg_ni == 0:
        return _pillar_pass_fail(
            "PE Ratio (5-Year)",
            None,
            pe_max,
            None,
            "value < threshold",
            "Average net income is zero, PE ratio undefined",
            {"market_cap": market_cap, "avg_net_income": avg_ni},
        )

    pe = market_cap / avg_ni
    passed = pe < pe_max
    log = f"market_cap ({market_cap:,.0f}) / avg_net_income ({avg_ni:,.0f}) = {pe:.1f}"

    return _pillar_pass_fail(
        "PE Ratio (5-Year)",
        pe,
        pe_max,
        passed,
        "value < threshold",
        log,
        {
            "market_cap": market_cap,
            "avg_net_income": avg_ni,
            "net_income_series": net_income,
        },
    )


def _calculate_roic(
    ebit_arr: List[Optional[float]],
    tax_arr: List[Optional[float]],
    pre_tax_arr: List[Optional[float]],
    total_debt_arr: List[Optional[float]],
    total_equity_arr: List[Optional[float]],
    total_cash_arr: List[Optional[float]],
    roic_min: float,
) -> Dict[str, Any]:
    """Pillar 2: ROIC = EBIT*(1-tax_rate) / (debt + equity - cash).

    Uses most recent year of data. Tax rate is computed from
    tax_expense / pre_tax_income if available, otherwise falls back to
    a reasonable estimate or skips.
    """
    valid_ebit = _filter_valid(ebit_arr)
    if not valid_ebit:
        return _pillar_pass_fail(
            "Return on Invested Capital (ROIC)",
            None,
            roic_min,
            None,
            "value > threshold",
            "EBIT data not available",
            {},
        )

    ebit = valid_ebit[-1]

    valid_debt = _filter_valid(total_debt_arr)
    valid_equity = _filter_valid(total_equity_arr)
    valid_cash = _filter_valid(total_cash_arr)

    debt = valid_debt[-1] if valid_debt else None
    equity = valid_equity[-1] if valid_equity else None
    cash = valid_cash[-1] if valid_cash else 0

    if debt is None or equity is None:
        return _pillar_pass_fail(
            "Return on Invested Capital (ROIC)",
            None,
            roic_min,
            None,
            "value > threshold",
            "Debt or equity data not available for invested capital",
            {
                "ebit": ebit,
                "total_debt": debt,
                "total_equity": equity,
                "total_cash": cash,
            },
        )

    invested_capital = debt + equity - cash
    if invested_capital <= 0:
        return _pillar_pass_fail(
            "Return on Invested Capital (ROIC)",
            None,
            roic_min,
            None,
            "value > threshold",
            "Invested capital is zero or negative",
            {"invested_capital": invested_capital},
        )

    valid_tax = _filter_valid(tax_arr)
    valid_pre_tax = _filter_valid(pre_tax_arr)

    tax_rate = None
    if valid_tax and valid_pre_tax and valid_pre_tax[-1] and valid_pre_tax[-1] != 0:
        tax_rate = valid_tax[-1] / valid_pre_tax[-1]
        tax_rate = max(0.0, min(tax_rate, 1.0))
    else:
        tax_rate = None

    if tax_rate is None:
        return _pillar_pass_fail(
            "Return on Invested Capital (ROIC)",
            None,
            roic_min,
            None,
            "value > threshold",
            "Tax rate could not be computed (missing tax expense or pre-tax income)",
            {
                "ebit": ebit,
                "tax_expense": valid_tax[-1] if valid_tax else None,
                "pre_tax_income": valid_pre_tax[-1] if valid_pre_tax else None,
            },
        )

    nopat = ebit * (1 - tax_rate)
    roic = (nopat / invested_capital) * 100
    passed = roic > roic_min
    log = (
        f"NOPAT = EBIT ({ebit:,.0f}) "
        f"× (1 - tax_rate ({tax_rate:.2f})) = {nopat:,.0f}; "
        f"Invested Capital = debt ({debt:,.0f}) "
        f"+ equity ({equity:,.0f}) "
        f"- cash ({cash:,.0f}) "
        f"= {invested_capital:,.0f}; "
        f"ROIC = {roic:.1f}%"
    )

    return _pillar_pass_fail(
        "Return on Invested Capital (ROIC)",
        roic,
        roic_min,
        passed,
        "value > threshold",
        log,
        {
            "ebit": ebit,
            "tax_rate": round(tax_rate, 4),
            "nopat": round(nopat, 2),
            "invested_capital": round(invested_capital, 2),
            "total_debt": debt,
            "total_equity": equity,
            "total_cash": cash,
        },
    )


def _calculate_growth_pillar(
    name: str,
    values: List[Optional[float]],
    comparison: str,
) -> Dict[str, Any]:
    """Generic growth pillar: CAGR of the given values must be positive."""
    cagr = _compute_cagr_pct(values)

    if cagr is None:
        valid = _filter_valid(values)
        return _pillar_pass_fail(
            name,
            None,
            0.0,
            None,
            comparison,
            f"Insufficient data for CAGR calculation "
            f"({len(valid)} valid data points, need 2)",
            {"values": values},
        )

    passed = cagr > 0
    valid = _filter_valid(values)
    log = (
        f"CAGR from {valid[0]:,.0f} to {valid[-1]:,.0f} "
        f"over {len(valid) - 1} years = {cagr:.1f}%"
    )

    return _pillar_pass_fail(
        name,
        cagr,
        0.0,
        passed,
        comparison,
        log,
        {
            "first_value": valid[0],
            "last_value": valid[-1],
            "years": len(valid) - 1,
            "values": values,
        },
    )


def _calculate_shares_trend(
    shares_arr: List[Optional[float]],
) -> Dict[str, Any]:
    """Pillar 5: Shares Outstanding Trend.

    Compares first and last year of Basic Average Shares.
    Pass if shares are decreasing (buybacks).
    """
    valid = _filter_valid(shares_arr)

    if len(valid) < 2:
        return _pillar_pass_fail(
            "Shares Outstanding Trend",
            None,
            0.0,
            None,
            "shares decreasing",
            f"Insufficient data ({len(valid)} valid data points, need 2)",
            {"basic_average_shares": shares_arr},
        )

    first = valid[0]
    last = valid[-1]

    if first == 0:
        return _pillar_pass_fail(
            "Shares Outstanding Trend",
            None,
            0.0,
            None,
            "shares decreasing",
            "First year shares is zero",
            {"basic_average_shares": shares_arr},
        )

    change_pct = ((last - first) / first) * 100
    passed = last < first
    direction = "decreasing (buybacks)" if passed else "increasing (dilution)"
    log = f"Shares: {first:,.0f} → {last:,.0f} " f"({change_pct:+.1f}%, {direction})"

    return _pillar_pass_fail(
        "Shares Outstanding Trend",
        change_pct,
        0.0,
        passed,
        "shares decreasing",
        log,
        {
            "first_year": first,
            "last_year": last,
            "change_pct": round(change_pct, 2),
            "basic_average_shares": shares_arr,
        },
    )


def _calculate_debt_to_fcf(
    lt_debt_arr: List[Optional[float]],
    fcf_arr: List[Optional[float]],
    max_multiple: float,
) -> Dict[str, Any]:
    """Pillar 6: Long-Term Liabilities.

    long_term_debt / avg(5yr FCF) must be < max_multiple.
    """
    valid_debt = _filter_valid(lt_debt_arr)
    avg_fcf = _safe_avg(fcf_arr)

    if not valid_debt:
        return _pillar_pass_fail(
            "Long-Term Liabilities",
            None,
            max_multiple,
            None,
            "debt/avg_fcf < threshold",
            "Long-term debt data not available",
            {"long_term_debt": lt_debt_arr, "avg_fcf": avg_fcf},
        )

    lt_debt = valid_debt[-1]

    if avg_fcf is None or avg_fcf <= 0:
        return _pillar_pass_fail(
            "Long-Term Liabilities",
            None,
            max_multiple,
            None,
            "debt/avg_fcf < threshold",
            "Average FCF is zero, negative, or unavailable",
            {"long_term_debt": lt_debt, "avg_fcf": avg_fcf},
        )

    multiple = lt_debt / avg_fcf
    passed = multiple < max_multiple
    log = (
        f"LT Debt ({lt_debt:,.0f}) / avg FCF ({avg_fcf:,.0f}) = {multiple:.1f}x "
        f"(limit: {max_multiple}x)"
    )

    return _pillar_pass_fail(
        "Long-Term Liabilities",
        multiple,
        max_multiple,
        passed,
        "debt/avg_fcf < threshold",
        log,
        {"long_term_debt": lt_debt, "avg_fcf": avg_fcf, "fcf_series": fcf_arr},
    )


def _calculate_fcf_multiple(
    market_cap: Optional[float],
    fcf_arr: List[Optional[float]],
    fcf_multiple: float,
) -> Dict[str, Any]:
    """Pillar 8: FCF Multiple.

    Compare market_cap to (avg_fcf × multiple).
    Pass if market_cap < calculated_value (undervalued by this metric).
    """
    avg_fcf = _safe_avg(fcf_arr)

    if market_cap is None or avg_fcf is None:
        return _pillar_pass_fail(
            "FCF Multiple",
            None,
            fcf_multiple,
            None,
            "market_cap < avg_fcf × threshold",
            "Market cap or FCF data not available",
            {"market_cap": market_cap, "avg_fcf": avg_fcf},
        )

    calculated_value = avg_fcf * fcf_multiple
    ratio = market_cap / calculated_value if calculated_value != 0 else None

    if ratio is None:
        return _pillar_pass_fail(
            "FCF Multiple",
            None,
            fcf_multiple,
            None,
            "market_cap < avg_fcf × threshold",
            "Calculated FCF value is zero",
            {"market_cap": market_cap, "avg_fcf": avg_fcf},
        )

    passed = market_cap < calculated_value
    log = (
        f"Market Cap ({market_cap:,.0f}) vs "
        f"avg FCF ({avg_fcf:,.0f}) × {fcf_multiple} = {calculated_value:,.0f}; "
        f"ratio = {ratio:.2f}"
    )

    return _pillar_pass_fail(
        "FCF Multiple",
        ratio,
        1.0,
        passed,
        "market_cap < avg_fcf × threshold (ratio < 1.0)",
        log,
        {
            "market_cap": market_cap,
            "avg_fcf": avg_fcf,
            "fcf_multiple_used": fcf_multiple,
            "calculated_value": round(calculated_value, 2),
            "fcf_series": fcf_arr,
        },
    )
