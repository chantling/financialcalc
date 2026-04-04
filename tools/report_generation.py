"""Report generation for financial analysis.

This module generates complete analysis reports with executive summary
at the top and confidence scores for quick review.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from financialcalc.utils.error_handling import ValidationError
from financialcalc.utils.session_manager import session_manager
from financialcalc.utils.validation import (
    assess_valuation,
    calculate_confidence_score,
    validate_assumptions,
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


def generate_analysis_report(
    session_id: str,
    qualitative_assessment: Optional[Dict[str, Any]] = None,
    include_sensitivity: bool = True,
    sensitivity_variables: Optional[List[str]] = None,
    sensitivity_ranges: Optional[List[List[float]]] = None,
) -> Dict[str, Any]:
    """Generate a complete analysis report from session data.
    
    The report includes:
    1. Executive summary with confidence score (at the top for quick review)
    2. Valuation assessment
    3. Assumptions used
    4. Detailed calculation results
    5. Validation warnings
    6. Sensitivity analysis (optional)
    
    Args:
        session_id: Session identifier (typically ticker symbol)
        qualitative_assessment: Optional dict with qualitative factors:
            - business_quality: "poor", "average", "good", "excellent"
            - moat_strength: "none", "narrow", "wide"
            - management_quality: "poor", "average", "good", "excellent"
            - financial_health: "poor", "average", "good", "excellent"
            - notes: Additional qualitative notes
        include_sensitivity: Whether to include sensitivity analysis
        sensitivity_variables: Variables to test (default: growth_rate, discount_rate)
        sensitivity_ranges: Value ranges for each variable
    
    Returns:
        Complete analysis report with executive summary at the top
    """
    session = _get_session_or_error(session_id)
    
    if not session.get("dcf_results"):
        raise ValidationError(
            "No DCF results in session. Run run_dcf_analysis first."
        )
    
    dcf_results = session["dcf_results"]
    current_metrics = session.get("current_metrics") or {}
    financial_data = session.get("financial_data") or {}
    
    # Get current price
    current_price = current_metrics.get("current_price")
    
    # Get historical CAGR for confidence calculation
    historical_cagr = None
    calculated_cagr = session.get("calculated_cagr")
    if calculated_cagr:
        historical_cagr = calculated_cagr.get("cagr")
    
    # Get company revenue for validation
    revenue_data = financial_data.get("revenue", [])
    company_revenue = None
    if revenue_data:
        valid_revenue = [r for r in revenue_data if r is not None]
        if valid_revenue:
            company_revenue = valid_revenue[-1]
    
    # Calculate confidence score
    confidence = calculate_confidence_score(
        growth_rates=dcf_results.get("growth_rates", []),
        terminal_multiple=dcf_results.get("terminal_multiple", 0),
        discount_rate=dcf_results.get("discount_rate", 0),
        historical_cagr=historical_cagr,
        company_revenue=company_revenue,
    )
    
    # Assess valuation
    intrinsic_value = dcf_results.get("intrinsic_value")
    valuation_assessment = assess_valuation(current_price, intrinsic_value)
    
    # Build executive summary (AT THE TOP for quick review)
    executive_summary = {
        "symbol": session.get("symbol", session_id),
        "analysis_date": datetime.now().strftime("%Y-%m-%d"),
        "current_price": current_price,
        "intrinsic_value": intrinsic_value,
        "buy_price": dcf_results.get("buy_price"),
        "margin_of_safety": dcf_results.get("margin_of_safety"),
        "confidence_score": confidence["confidence_score"],
        "confidence_level": confidence["confidence_level"],
        "confidence_interpretation": confidence["interpretation"],
        "risk_factors": confidence["risk_factors"],
        "valuation_assessment": valuation_assessment["assessment"],
        "price_to_iv_ratio": valuation_assessment["price_to_iv_ratio"],
        "valuation_interpretation": valuation_assessment["interpretation"],
    }
    
    # Build detailed sections
    report = {
        "executive_summary": executive_summary,  # FIRST for quick review
        "qualitative_assessment": qualitative_assessment or {},
        "assumptions": {
            "base_fcf": dcf_results.get("base_fcf"),
            "base_fcf_method": dcf_results.get("base_fcf_method"),
            "growth_rates": dcf_results.get("growth_rates"),
            "discount_rate": dcf_results.get("discount_rate"),
            "terminal_multiple": dcf_results.get("terminal_multiple"),
            "projection_years": len(dcf_results.get("growth_rates", [])),
            "margin_of_safety": dcf_results.get("margin_of_safety"),
        },
        "valuation_results": {
            "projected_cash_flows": dcf_results.get("projected_cash_flows"),
            "present_value_flows": dcf_results.get("present_value_flows"),
            "terminal_value": dcf_results.get("terminal_value"),
            "present_value_terminal": dcf_results.get("present_value_terminal"),
            "enterprise_value": dcf_results.get("enterprise_value"),
            "net_cash": dcf_results.get("net_cash"),
            "total_equity_value": dcf_results.get("total_equity_value"),
            "shares_outstanding": dcf_results.get("shares_outstanding"),
            "intrinsic_value": intrinsic_value,
            "buy_price": dcf_results.get("buy_price"),
        },
        "validation_warnings": dcf_results.get("validation_warnings", []),
        "historical_reference": {
            "historical_cagr": historical_cagr,
            "available_fcf_years": len([v for v in financial_data.get("free_cash_flow", []) if v is not None]),
            "available_revenue_years": len([v for v in revenue_data if v is not None]),
        },
    }
    
    # Add sensitivity analysis if requested
    if include_sensitivity:
        from financialcalc.tools.analysis_support import calculate_sensitivity_analysis
        
        # Use default ranges if not specified
        if sensitivity_variables is None:
            sensitivity_variables = ["growth_rate", "discount_rate"]
        if sensitivity_ranges is None:
            sensitivity_ranges = [[-3, 0, 3], [-2, 0, 2]]
        
        try:
            sensitivity = calculate_sensitivity_analysis(
                base_value=intrinsic_value,
                variables=sensitivity_variables,
                ranges=sensitivity_ranges,
                dcf_params={
                    "base_fcf": dcf_results.get("base_fcf"),
                    "growth_rates": dcf_results.get("growth_rates"),
                    "discount_rate": dcf_results.get("discount_rate"),
                    "terminal_multiple": dcf_results.get("terminal_multiple"),
                    "shares_outstanding": dcf_results.get("shares_outstanding"),
                    "net_cash": dcf_results.get("net_cash"),
                },
            )
            report["sensitivity_analysis"] = sensitivity
        except Exception as e:
            logger.warning(f"Sensitivity analysis failed: {e}")
            report["sensitivity_analysis"] = {"error": str(e)}
    
    return report


def generate_scenario_report(
    session_id: str,
    scenarios: List[Dict[str, Any]],
    qualitative_assessment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate a report with scenario analysis.
    
    Args:
        session_id: Session identifier
        scenarios: List of scenario dicts, each with:
            - name: Scenario name
            - growth_rates: List of growth rates (10 years)
            - terminal_multiple: Terminal multiple
            - probability: Probability weight (0-100)
        qualitative_assessment: Optional qualitative factors
    
    Returns:
        Complete report with scenario analysis
    """
    session = _get_session_or_error(session_id)
    
    if not session.get("dcf_results"):
        raise ValidationError("No DCF results in session. Run run_dcf_analysis first.")
    
    dcf_results = session["dcf_results"]
    current_metrics = session.get("current_metrics") or {}
    financial_data = session.get("financial_data") or {}
    
    current_price = current_metrics.get("current_price")
    
    # Get historical data for confidence
    historical_cagr = None
    calculated_cagr = session.get("calculated_cagr")
    if calculated_cagr:
        historical_cagr = calculated_cagr.get("cagr")
    
    revenue_data = financial_data.get("revenue", [])
    company_revenue = None
    if revenue_data:
        valid_revenue = [r for r in revenue_data if r is not None]
        if valid_revenue:
            company_revenue = valid_revenue[-1]
    
    # Calculate confidence for base case
    base_confidence = calculate_confidence_score(
        growth_rates=dcf_results.get("growth_rates", []),
        terminal_multiple=dcf_results.get("terminal_multiple", 0),
        discount_rate=dcf_results.get("discount_rate", 0),
        historical_cagr=historical_cagr,
        company_revenue=company_revenue,
    )
    
    # Run scenario analysis
    from financialcalc.tools.analysis_support import calculate_probability_weighted
    
    scenario_results = []
    scenario_values = []
    scenario_probabilities = []
    
    for scenario in scenarios:
        # Run DCF for this scenario
        from financialcalc.tools.core_calculations import (
            calculate_intrinsic_value,
            calculate_present_value,
            calculate_terminal_value,
            project_cash_flows,
        )
        
        try:
            proj = project_cash_flows(
                dcf_results["base_fcf"],
                scenario["growth_rates"],
                len(scenario["growth_rates"]),
            )
            pv = calculate_present_value(proj["projected_flows"], dcf_results["discount_rate"])
            tv = calculate_terminal_value(proj["projected_flows"][-1], scenario["terminal_multiple"])
            
            # Discount terminal value
            r = dcf_results["discount_rate"] / 100
            years = len(scenario["growth_rates"])
            tv_pv = tv["terminal_value"] / (1 + r) ** years
            
            iv = calculate_intrinsic_value(
                pv["total_pv"],
                tv_pv,
                dcf_results.get("net_cash", 0),
                dcf_results["shares_outstanding"],
            )
            
            scenario_iv = iv["intrinsic_value"]
            scenario_buy_price = scenario_iv * (1 - dcf_results.get("margin_of_safety", 30) / 100)
            
            # Calculate confidence for this scenario
            scenario_confidence = calculate_confidence_score(
                growth_rates=scenario["growth_rates"],
                terminal_multiple=scenario["terminal_multiple"],
                discount_rate=dcf_results["discount_rate"],
                historical_cagr=historical_cagr,
                company_revenue=company_revenue,
            )
            
            scenario_results.append({
                "name": scenario["name"],
                "assumptions": {
                    "growth_rates": scenario["growth_rates"],
                    "terminal_multiple": scenario["terminal_multiple"],
                    "probability": scenario["probability"],
                },
                "intrinsic_value": round(scenario_iv, 2),
                "buy_price": round(scenario_buy_price, 2),
                "confidence_score": scenario_confidence["confidence_score"],
                "confidence_level": scenario_confidence["confidence_level"],
            })
            
            scenario_values.append(scenario_iv)
            scenario_probabilities.append(scenario["probability"])
            
        except Exception as e:
            logger.warning(f"Scenario '{scenario['name']}' failed: {e}")
            scenario_results.append({
                "name": scenario["name"],
                "error": str(e),
            })
    
    # Calculate probability-weighted IV
    weighted_result = None
    if scenario_values and scenario_probabilities:
        try:
            weighted_result = calculate_probability_weighted(scenario_values, scenario_probabilities)
        except Exception as e:
            logger.warning(f"Probability weighting failed: {e}")
    
    # Assess valuation using weighted IV
    weighted_iv = weighted_result["weighted_average"] if weighted_result else None
    valuation_assessment = assess_valuation(current_price, weighted_iv)
    
    # Build executive summary
    executive_summary = {
        "symbol": session.get("symbol", session_id),
        "analysis_date": datetime.now().strftime("%Y-%m-%d"),
        "current_price": current_price,
        "base_case_iv": dcf_results.get("intrinsic_value"),
        "weighted_iv": weighted_iv,
        "weighted_buy_price": round(weighted_iv * (1 - dcf_results.get("margin_of_safety", 30) / 100), 2) if weighted_iv else None,
        "margin_of_safety": dcf_results.get("margin_of_safety"),
        "base_case_confidence_score": base_confidence["confidence_score"],
        "base_case_confidence_level": base_confidence["confidence_level"],
        "valuation_assessment": valuation_assessment["assessment"],
        "price_to_iv_ratio": valuation_assessment["price_to_iv_ratio"],
        "valuation_interpretation": valuation_assessment["interpretation"],
        "scenario_range": {
            "min": min(s["intrinsic_value"] for s in scenario_results if "intrinsic_value" in s) if scenario_results else None,
            "max": max(s["intrinsic_value"] for s in scenario_results if "intrinsic_value" in s) if scenario_results else None,
        },
    }
    
    report = {
        "executive_summary": executive_summary,
        "qualitative_assessment": qualitative_assessment or {},
        "base_case_assumptions": {
            "base_fcf": dcf_results.get("base_fcf"),
            "growth_rates": dcf_results.get("growth_rates"),
            "discount_rate": dcf_results.get("discount_rate"),
            "terminal_multiple": dcf_results.get("terminal_multiple"),
        },
        "scenario_analysis": {
            "scenarios": scenario_results,
            "probability_weighted": weighted_result,
        },
        "base_case_results": {
            "intrinsic_value": dcf_results.get("intrinsic_value"),
            "buy_price": dcf_results.get("buy_price"),
        },
        "validation_warnings": dcf_results.get("validation_warnings", []),
    }
    
    return report