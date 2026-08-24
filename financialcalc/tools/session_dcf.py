"""Session-based DCF analysis tools.

This module provides DCF analysis tools that use session storage to maintain
state between tool calls, eliminating the need for agents to pass data between tools.
"""

import logging
from typing import Any, Dict, List, Optional

from financialcalc.tools.assumption_registry import (
    _read_registry_row,
    normalize_ticker,
)
from financialcalc.tools.core_calculations import (
    calculate_cagr,
    calculate_intrinsic_value,
    calculate_present_value,
    calculate_terminal_value,
    project_cash_flows,
)
from financialcalc.utils.error_handling import ValidationError
from financialcalc.utils.session_manager import session_manager
from financialcalc.utils.validation import (
    assess_valuation,
    calculate_confidence_score,
    validate_assumptions,
    validate_against_registry,
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


def calculate_base_fcf(
    financial_data: Dict[str, Any],
    method: str = "most_recent",
) -> Dict[str, Any]:
    """Calculate base FCF using specified method.
    
    Args:
        financial_data: Financial data from get_financial_data
        method: Method for calculating base FCF:
            - "most_recent": Most recent year FCF
            - "average": 3-year average FCF
            - "median": Median FCF over available years
            - "sbc_adjusted": FCF minus stock-based compensation
            - "normalized": Exclude outliers then average
    
    Returns:
        Dictionary with base_fcf, method_used, and calculation details
    """
    fcf_values = financial_data.get("free_cash_flow", [])
    sbc_values = financial_data.get("stock_based_compensation", [])
    
    # Filter out None values
    valid_fcf = [v for v in fcf_values if v is not None]
    
    if not valid_fcf:
        raise ValidationError("No valid FCF data available")
    
    if method == "most_recent":
        base_fcf = valid_fcf[-1]
        description = "Most recent year FCF"
        years_used = 1
        
    elif method == "average":
        if len(valid_fcf) < 3:
            # Not enough data for 3-year average, use what's available
            base_fcf = sum(valid_fcf) / len(valid_fcf)
            description = f"Average FCF ({len(valid_fcf)} years)"
            years_used = len(valid_fcf)
        else:
            base_fcf = sum(valid_fcf[-3:]) / 3
            description = "3-year average FCF"
            years_used = 3
            
    elif method == "median":
        sorted_fcf = sorted(valid_fcf)
        n = len(sorted_fcf)
        if n % 2 == 0:
            base_fcf = (sorted_fcf[n//2-1] + sorted_fcf[n//2]) / 2
        else:
            base_fcf = sorted_fcf[n//2]
        description = "Median FCF over available years"
        years_used = n
        
    elif method == "sbc_adjusted":
        # Get most recent SBC-adjusted FCF
        if sbc_values:
            valid_sbc = [v for v in sbc_values if v is not None]
            if valid_sbc:
                # Find most recent year where both FCF and SBC exist
                for i in range(len(fcf_values) - 1, -1, -1):
                    if fcf_values[i] is not None and i < len(sbc_values) and sbc_values[i] is not None:
                        base_fcf = fcf_values[i] - sbc_values[i]
                        description = "Most recent SBC-adjusted FCF"
                        years_used = 1
                        break
                else:
                    # No matching SBC data, fall back to most recent FCF
                    base_fcf = valid_fcf[-1]
                    description = "Most recent FCF (SBC data not available)"
                    years_used = 1
            else:
                base_fcf = valid_fcf[-1]
                description = "Most recent FCF (SBC data not available)"
                years_used = 1
        else:
            base_fcf = valid_fcf[-1]
            description = "Most recent FCF (SBC data not available)"
            years_used = 1
            
    elif method == "normalized":
        if len(valid_fcf) < 3:
            # Not enough data to exclude outliers
            base_fcf = sum(valid_fcf) / len(valid_fcf)
            description = f"Average FCF ({len(valid_fcf)} years, insufficient for outlier exclusion)"
            years_used = len(valid_fcf)
        else:
            sorted_fcf = sorted(valid_fcf)
            trimmed = sorted_fcf[1:-1]  # Remove highest and lowest
            base_fcf = sum(trimmed) / len(trimmed)
            description = "Normalized FCF (excluding outliers)"
            years_used = len(trimmed)
    else:
        raise ValidationError(f"Unknown base FCF method: {method}")
    
    return {
        "base_fcf": round(base_fcf, 2),
        "method": method,
        "description": description,
        "years_used": years_used,
        "available_fcf_years": len(valid_fcf),
    }


def get_base_fcf_options(session_id: str) -> Dict[str, Any]:
    """Get all available base FCF calculation options.
    
    This tool helps the agent choose the most appropriate base FCF
    for the analysis by presenting all available options with guidance.
    
    Args:
        session_id: Session identifier (typically ticker symbol)
    
    Returns:
        Dictionary with all available base FCF options and guidance
    """
    session = _get_session_or_error(session_id)
    
    if not session.get("financial_data"):
        raise ValidationError(
            "No financial data in session. Call get_financial_data first."
        )
    
    financial_data = session["financial_data"]
    
    # Calculate all available options
    options = {}
    
    # Try each method
    for method in ["most_recent", "average", "median", "sbc_adjusted", "normalized"]:
        try:
            result = calculate_base_fcf(financial_data, method)
            options[method] = result
        except Exception as e:
            logger.debug(f"Could not calculate {method} base FCF: {e}")
    
    # Get historical CAGR for reference
    cagr_result = None
    if "revenue" in financial_data:
        try:
            cagr_result = calculate_cagr(financial_data["revenue"])
            # Store CAGR in session
            session_manager.update_session(session_id, {"calculated_cagr": cagr_result})
        except Exception as e:
            logger.debug(f"Could not calculate revenue CAGR: {e}")
    
    # Get net cash/debt position
    net_cash = 0
    balance_sheet = session.get("balance_sheet")
    if balance_sheet:
        total_cash = balance_sheet.get("total_cash", [None])[-1] if balance_sheet.get("total_cash") else None
        total_debt = balance_sheet.get("total_debt", [None])[-1] if balance_sheet.get("total_debt") else None
        if total_cash is not None and total_debt is not None:
            net_cash = total_cash - total_debt
    
    # Get shares outstanding (prefer diluted for DCF)
    shares = None
    diluted_shares = None
    current_metrics = session.get("current_metrics")
    if current_metrics:
        diluted_shares = current_metrics.get("diluted_shares_outstanding")
        shares = current_metrics.get("shares_outstanding")
    
    # Generate guidance
    guidance = []
    
    if options:
        # Determine recommended method based on data characteristics
        fcf_values = [v for v in financial_data.get("free_cash_flow", []) if v is not None]
        
        if len(fcf_values) >= 3:
            # Check volatility
            avg_fcf = sum(fcf_values) / len(fcf_values)
            max_deviation = max(abs(v - avg_fcf) / avg_fcf for v in fcf_values)
            
            if max_deviation > 0.3:
                guidance.append(
                    "FCF shows high volatility (>30% deviation from average). "
                    "Consider using 'median' or 'normalized' method to reduce outlier impact."
                )
            else:
                guidance.append(
                    "FCF is relatively stable. 'most_recent' or 'average' methods are appropriate."
                )
        
        if "sbc_adjusted" in options and options["sbc_adjusted"]["base_fcf"] < options["most_recent"]["base_fcf"] * 0.9:
            guidance.append(
                "Stock-based compensation is significant (>10% of FCF). "
                "Consider using 'sbc_adjusted' method for more conservative valuation."
            )
        
        if cagr_result and cagr_result.get("cagr"):
            guidance.append(
                f"Historical revenue CAGR: {cagr_result['cagr']:.1f}%. "
                "Use this as a reference when selecting growth rates."
            )
    
    result = {
        "session_id": session_id,
        "symbol": session.get("symbol", session_id),
        "options": options,
        "historical_cagr": cagr_result.get("cagr") if cagr_result else None,
        "net_cash": round(net_cash, 2),
        "shares_outstanding": shares,
        "diluted_shares_outstanding": diluted_shares or shares,
        "available_fcf_years": len([v for v in financial_data.get("free_cash_flow", []) if v is not None]),
        "guidance": guidance,
    }
    
    # Store in session for later use
    session_manager.update_session(session_id, {"base_fcf_options": result})
    
    return result


def run_dcf_analysis(
    session_id: str,
    growth_rates: List[float],
    terminal_multiple: float,
    discount_rate: float = 10.0,
    margin_of_safety: float = 30.0,
    base_fcf_method: str = "most_recent",
    net_cash_override: Optional[float] = None,
    shares_override: Optional[float] = None,
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Run DCF analysis using data from session.
    
    This is the primary DCF analysis tool. It pulls all required data from
    the session and performs calculations without the agent needing to pass
    data between tools.
    
    Assumptions are validated against the per-company registry (hard reject
    on deviation unless override_reason is supplied). Call
    get_prior_assumptions first to read the registered methodology.
    
    Args:
        session_id: Session identifier (typically ticker symbol)
        growth_rates: List of growth rates for each projection year
        terminal_multiple: Terminal value multiple (e.g., 15 for 15x)
        discount_rate: Discount rate as percentage (default: 10%)
        margin_of_safety: Margin of safety percentage (default: 30%)
        base_fcf_method: Method for calculating base FCF (default: "most_recent")
        net_cash_override: Override net cash/debt position (optional)
        shares_override: Override shares outstanding (optional)
        override_reason: Required to proceed when assumptions violate the
            registry guardrails; must cite the changed fundamental (optional)
    
    Returns:
        Complete DCF analysis results with validation warnings
    """
    session = _get_session_or_error(session_id)
    
    if not session.get("financial_data"):
        raise ValidationError(
            "No financial data in session. Call get_financial_data first."
        )
    
    financial_data = session["financial_data"]
    
    # Get historical CAGR for validation
    historical_cagr = None
    calculated_cagr = session.get("calculated_cagr")
    if calculated_cagr:
        historical_cagr = calculated_cagr.get("cagr")
    elif "revenue" in financial_data:
        try:
            cagr_result = calculate_cagr(financial_data["revenue"])
            historical_cagr = cagr_result.get("cagr")
            session_manager.update_session(session_id, {"calculated_cagr": cagr_result})
        except Exception:
            pass
    
    # Get company revenue for validation
    revenue_data = financial_data.get("revenue", [])
    company_revenue = None
    if revenue_data:
        valid_revenue = [r for r in revenue_data if r is not None]
        if valid_revenue:
            company_revenue = valid_revenue[-1]
    
    # Validate assumptions
    validation_warnings = validate_assumptions(
        growth_rates=growth_rates,
        terminal_multiple=terminal_multiple,
        discount_rate=discount_rate,
        historical_cagr=historical_cagr,
        company_revenue=company_revenue,
    )

    # Registry guardrails (hard reject unless override_reason supplied)
    canonical = normalize_ticker(session.get("symbol", session_id))
    registry_entry = _read_registry_row(canonical)
    wacc_baseline = None
    if registry_entry is None:
        try:
            from financialcalc.tools.wacc import calculate_wacc

            wacc_result = calculate_wacc(canonical)
            wacc_baseline = wacc_result.get("wacc")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"WACC calculation unavailable for {canonical}: {e}")

    violations = validate_against_registry(
        growth_rates,
        terminal_multiple,
        discount_rate,
        base_fcf_method=base_fcf_method,
        registry=registry_entry,
        wacc_baseline=wacc_baseline,
    )
    overridden = bool(violations and override_reason and override_reason.strip())
    registry_match = {
        "ticker": canonical,
        "registered": registry_entry is not None,
        "violations": violations,
        "overridden": overridden,
        "override_reason": override_reason if overridden else None,
    }
    if violations and not overridden:
        raise ValidationError(
            "Assumption guardrails violated: " + "; ".join(violations)
            + ". Re-run with assumptions matching the registry (see "
            "get_prior_assumptions), or pass override_reason citing the "
            "changed fundamental. After an accepted override, call "
            "register_assumptions to update the registry."
        )

    # Calculate base FCF
    base_fcf_result = calculate_base_fcf(financial_data, base_fcf_method)
    base_fcf = base_fcf_result["base_fcf"]
    
    # Get net cash/debt
    if net_cash_override is not None:
        net_cash = net_cash_override
    else:
        net_cash = 0
        balance_sheet = session.get("balance_sheet")
        if balance_sheet:
            total_cash = balance_sheet.get("total_cash", [None])[-1] if balance_sheet.get("total_cash") else None
            total_debt = balance_sheet.get("total_debt", [None])[-1] if balance_sheet.get("total_debt") else None
            if total_cash is not None and total_debt is not None:
                net_cash = total_cash - total_debt
    
    # Get shares outstanding — prefer diluted (per IV_Distilled.md methodology)
    shares_basis = "override"
    if shares_override is not None:
        shares = shares_override
    else:
        shares = None
        current_metrics = session.get("current_metrics")
        if current_metrics:
            shares = current_metrics.get("diluted_shares_outstanding")
            if shares:
                shares_basis = "diluted"
            if not shares:
                shares = current_metrics.get("shares_outstanding")
                if shares:
                    shares_basis = "basic"

        if not shares:
            # Try to get from financial data
            financial_data_shares = financial_data.get("diluted_shares_outstanding")
            if financial_data_shares:
                shares = financial_data_shares
                shares_basis = "diluted"
            else:
                shares = financial_data.get("shares_outstanding")
                if shares:
                    shares_basis = "basic"

        if not shares:
            raise ValidationError(
                "Shares outstanding not available. Provide shares_override or "
                "ensure get_current_metrics was called."
            )

    assert shares is not None
    # Run DCF calculations
    years = len(growth_rates)
    
    # Project cash flows
    projection = project_cash_flows(base_fcf, growth_rates, years)
    
    # Calculate present value of projected cash flows
    pv_flows = calculate_present_value(projection["projected_flows"], discount_rate)
    
    # Calculate terminal value
    final_year_fcf = projection["projected_flows"][-1]
    tv_result = calculate_terminal_value(final_year_fcf, terminal_multiple)
    
    # Discount terminal value
    r = discount_rate / 100
    tv_pv = tv_result["terminal_value"] / (1 + r) ** years
    
    # Calculate intrinsic value
    iv_result = calculate_intrinsic_value(
        pv_flows["total_pv"],
        tv_pv,
        net_cash,
        shares,
    )
    
    # Calculate buy price with margin of safety
    intrinsic_value = iv_result["intrinsic_value"]
    buy_price = intrinsic_value * (1 - margin_of_safety / 100)
    
    # Calculate confidence score
    confidence = calculate_confidence_score(
        growth_rates=growth_rates,
        terminal_multiple=terminal_multiple,
        discount_rate=discount_rate,
        historical_cagr=historical_cagr,
        company_revenue=company_revenue,
    )
    
    # Assess valuation
    current_price = None
    current_metrics = session.get("current_metrics")
    if current_metrics:
        current_price = current_metrics.get("current_price")
    
    valuation = assess_valuation(current_price, intrinsic_value)
    
    # Build results
    results = {
        "symbol": session.get("symbol", session_id),
        "base_fcf": base_fcf,
        "base_fcf_method": base_fcf_method,
        "base_fcf_description": base_fcf_result["description"],
        "growth_rates": growth_rates,
        "discount_rate": discount_rate,
        "terminal_multiple": terminal_multiple,
        "margin_of_safety": margin_of_safety,
        "projection_years": years,
        "projected_cash_flows": projection["projected_flows"],
        "present_value_flows": round(pv_flows["total_pv"], 2),
        "terminal_value": tv_result["terminal_value"],
        "present_value_terminal": round(tv_pv, 2),
        "enterprise_value": iv_result["enterprise_value"],
        "net_cash": round(net_cash, 2),
        "total_equity_value": round(iv_result["enterprise_value"] + net_cash, 2),
        "shares_outstanding": shares,
        "shares_basis": shares_basis,
        "intrinsic_value": round(intrinsic_value, 2),
        "buy_price": round(buy_price, 2),
        "current_price": current_price,
        "confidence": confidence,
        "valuation": valuation,
        "validation_warnings": validation_warnings,
        "registry_match": registry_match,
        "wacc_baseline": wacc_baseline,
        "calculation_log": [
            f"Registry: {'matched' if registry_match['registered'] and not violations else 'n/a'}"
            + (f" (OVERRIDDEN: {override_reason})" if overridden else ""),
            f"Base FCF ({base_fcf_method}): ${base_fcf/1e9:.2f}B",
            f"Growth rates: {growth_rates}",
            f"Discount rate: {discount_rate}%",
            f"Terminal multiple: {terminal_multiple}x",
            f"Year {years} FCF: ${final_year_fcf/1e9:.2f}B",
            f"PV of cash flows: ${pv_flows['total_pv']/1e9:.2f}B",
            f"Terminal value: ${tv_result['terminal_value']/1e9:.2f}B",
            f"PV of terminal value: ${tv_pv/1e9:.2f}B",
            f"Enterprise value: ${iv_result['enterprise_value']/1e9:.2f}B",
            f"Net cash/debt: ${net_cash/1e9:.2f}B",
            f"Equity value: ${(iv_result['enterprise_value'] + net_cash)/1e9:.2f}B",
            f"Shares outstanding ({shares_basis}): {shares/1e6:.1f}M",
            f"Intrinsic value per share: ${intrinsic_value:.2f}",
            f"Buy price ({margin_of_safety}% MoS): ${buy_price:.2f}",
        ],
    }
    
    # Store in session
    session_manager.update_session(session_id, {"dcf_results": results})
    
    return results
