"""Reasonableness validation for financial analysis assumptions.

This module provides validation functions that check assumptions against
reasonable ranges and calculate confidence scores for valuations.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def validate_growth_rates(
    growth_rates: List[float],
    historical_cagr: Optional[float] = None,
    company_revenue: Optional[float] = None,
) -> List[str]:
    """Validate growth rate assumptions and return warnings.

    Args:
        growth_rates: List of growth rates for projection years
        historical_cagr: Historical CAGR for comparison
        company_revenue: Most recent revenue for size-based checks

    Returns:
        List of warning messages (empty if all assumptions are reasonable)
    """
    warnings: List[str] = []

    if not growth_rates:
        return warnings

    # Check for extreme growth rates
    for i, rate in enumerate(growth_rates):
        if rate > 50:
            warnings.append(
                f"Warning: Year {i+1} growth rate ({rate}%) is extremely high (>50%). "
                "This is rarely sustainable for any company."
            )
        elif rate < -30:
            warnings.append(
                f"Warning: Year {i+1} growth rate ({rate}%) indicates severe decline (<-30%). "
                "Consider if this is realistic or overly pessimistic."
            )

    # Check average growth against historical
    if historical_cagr and historical_cagr > 0:
        avg_growth = sum(growth_rates[:5]) / min(5, len(growth_rates))
        deviation = (avg_growth / historical_cagr - 1) * 100

        if deviation > 100:
            warnings.append(
                f"Warning: Average Years 1-5 growth ({avg_growth:.1f}%) is {deviation:.0f}% higher than "
                f"historical CAGR ({historical_cagr:.1f}%). This is an aggressive assumption."
            )
        elif deviation > 50:
            warnings.append(
                f"Note: Average Years 1-5 growth ({avg_growth:.1f}%) is {deviation:.0f}% higher than "
                f"historical CAGR ({historical_cagr:.1f}%). Justify this acceleration."
            )
        elif deviation < -50:
            warnings.append(
                f"Note: Average Years 1-5 growth ({avg_growth:.1f}%) is {abs(deviation):.0f}% lower than "
                f"historical CAGR ({historical_cagr:.1f}%). This is a very conservative assumption."
            )

    # Check large company growth constraints
    if company_revenue and company_revenue > 100_000_000_000:  # $100B+
        max_growth = (
            max(growth_rates[:5]) if len(growth_rates) >= 5 else max(growth_rates)
        )
        if max_growth > 25:
            warnings.append(
                f"Warning: {max_growth}% growth rate is aggressive for a company with "
                f"${company_revenue/1e9:.0f}B revenue. The law of large numbers typically limits growth."
            )
    elif company_revenue and company_revenue > 50_000_000_000:  # $50B+
        max_growth = (
            max(growth_rates[:5]) if len(growth_rates) >= 5 else max(growth_rates)
        )
        if max_growth > 30:
            warnings.append(
                f"Warning: {max_growth}% growth rate is aggressive for a company with "
                f"${company_revenue/1e9:.0f}B revenue."
            )

    # Check two-period structure (if we have 10 years)
    if len(growth_rates) == 10:
        avg_y1_5 = sum(growth_rates[:5]) / 5
        avg_y6_10 = sum(growth_rates[5:]) / 5

        if avg_y6_10 > avg_y1_5:
            warnings.append(
                f"Warning: Years 6-10 average growth ({avg_y6_10:.1f}%) is higher than "
                f"Years 1-5 ({avg_y1_5:.1f}%). Typically growth decelerates over time."
            )

    return warnings


def validate_terminal_multiple(
    terminal_multiple: float,
    historical_pe_range: Optional[Tuple[float, float]] = None,
    business_quality: Optional[str] = None,
) -> List[str]:
    """Validate terminal multiple assumption and return warnings.

    Args:
        terminal_multiple: The terminal value multiple (e.g., 15 for 15x)
        historical_pe_range: Tuple of (min, max) historical P/E or P/FCF
        business_quality: Quality assessment ("poor", "average", "good", "excellent")

    Returns:
        List of warning messages
    """
    warnings = []

    # General range checks
    if terminal_multiple > 30:
        warnings.append(
            f"Warning: Terminal multiple ({terminal_multiple}x) is very high (>30x). "
            "This implies exceptional perpetual growth and is rarely justified."
        )
    elif terminal_multiple > 25:
        warnings.append(
            f"Warning: Terminal multiple ({terminal_multiple}x) is above typical range (8-25x). "
            "Ensure this is justified by exceptional business quality."
        )
    elif terminal_multiple < 5:
        warnings.append(
            f"Warning: Terminal multiple ({terminal_multiple}x) is very low (<5x). "
            "This implies a distressed or declining business."
        )
    elif terminal_multiple < 8:
        warnings.append(
            f"Note: Terminal multiple ({terminal_multiple}x) is below typical range (8-25x). "
            "This is conservative but may be appropriate for lower-quality businesses."
        )

    # Check against historical range
    if historical_pe_range:
        min_pe, max_pe = historical_pe_range
        avg_pe = (min_pe + max_pe) / 2

        if terminal_multiple > max_pe * 1.2:
            warnings.append(
                f"Warning: Terminal multiple ({terminal_multiple}x) exceeds historical range "
                f"({min_pe:.1f}x - {max_pe:.1f}x) by >20%."
            )
        elif terminal_multiple < min_pe * 0.8:
            warnings.append(
                f"Note: Terminal multiple ({terminal_multiple}x) is below historical range "
                f"({min_pe:.1f}x - {max_pe:.1f}x) by >20%."
            )

    # Business quality guidance
    if business_quality:
        quality_ranges = {
            "poor": (5, 10),
            "average": (10, 15),
            "good": (15, 20),
            "excellent": (20, 25),
        }

        if business_quality in quality_ranges:
            min_q, max_q = quality_ranges[business_quality]
            if terminal_multiple > max_q:
                warnings.append(
                    f"Note: Terminal multiple ({terminal_multiple}x) is high for a "
                    f"'{business_quality}' quality business (typical: {min_q}-{max_q}x)."
                )

    return warnings


def validate_discount_rate(discount_rate: float) -> List[str]:
    """Validate discount rate assumption and return warnings.

    Args:
        discount_rate: The discount rate as a percentage

    Returns:
        List of warning messages
    """
    warnings = []

    if discount_rate < 4:
        warnings.append(
            f"Warning: Discount rate ({discount_rate}%) is very low (<4%). "
            "This implies virtually no risk and is unrealistic for equities."
        )
    elif discount_rate < 6:
        warnings.append(
            f"Warning: Discount rate ({discount_rate}%) is below typical range (6-15%). "
            "Consider using a higher rate to account for equity risk."
        )
    elif discount_rate > 20:
        warnings.append(
            f"Warning: Discount rate ({discount_rate}%) is very high (>20%). "
            "This implies extreme risk and will result in very low valuations."
        )
    elif discount_rate > 15:
        warnings.append(
            f"Note: Discount rate ({discount_rate}%) is above typical range (6-15%). "
            "This is conservative but may be appropriate for high-risk investments."
        )

    return warnings


def validate_assumptions(
    growth_rates: List[float],
    terminal_multiple: float,
    discount_rate: float,
    historical_cagr: Optional[float] = None,
    company_revenue: Optional[float] = None,
    historical_pe_range: Optional[Tuple[float, float]] = None,
    business_quality: Optional[str] = None,
) -> List[str]:
    """Validate all DCF assumptions and return combined warnings.

    Args:
        growth_rates: List of growth rates for projection years
        terminal_multiple: The terminal value multiple
        discount_rate: The discount rate as a percentage
        historical_cagr: Historical CAGR for comparison
        company_revenue: Most recent revenue for size-based checks
        historical_pe_range: Tuple of (min, max) historical P/E or P/FCF
        business_quality: Quality assessment

    Returns:
        Combined list of warning messages
    """
    warnings = []

    warnings.extend(
        validate_growth_rates(growth_rates, historical_cagr, company_revenue)
    )

    warnings.extend(
        validate_terminal_multiple(
            terminal_multiple, historical_pe_range, business_quality
        )
    )

    warnings.extend(validate_discount_rate(discount_rate))

    return warnings


def validate_against_registry(
    growth_rates: List[float],
    terminal_multiple: float,
    discount_rate: float,
    base_fcf_method: Optional[str] = None,
    registry: Optional[Dict[str, Any]] = None,
    wacc_baseline: Optional[float] = None,
) -> List[str]:
    """Validate DCF assumptions against the registry / absolute guardrails.

    Hard-reject rules (callers raise ValidationError on any violation unless
    the agent supplies an override_reason):

    With a registry entry (update runs):
        - terminal multiple within ±1x of registered value
        - projection period (len(growth_rates)) equals registered period
        - discount rate within ±1pp of registered value
        - base_fcf_method equals registered method (when provided)
        - each growth year within ±3pp of the registered schedule

    Without a registry entry (first run):
        - discount rate within ±2pp of WACC baseline if available,
          otherwise within 9-11%
        - terminal multiple within 8-16x absolute band
        - first-year growth <= 1.5x historical CAGR (when known)
        - no year-over-year increase in the growth schedule > 5pp (fade only)
        - final-year growth <= 5%

    Args:
        growth_rates: Proposed per-year growth rates (percent)
        terminal_multiple: Proposed terminal multiple
        discount_rate: Proposed discount rate (percent)
        base_fcf_method: Proposed base FCF method (optional)
        registry: Registry entry dict (from assumption_registry) or None
        wacc_baseline: Calculated WACC (percent) or None

    Returns:
        List of violation messages (empty = assumptions accepted).
    """
    violations: List[str] = []

    if registry is not None:
        reg_tm = registry.get("terminal_multiple")
        if reg_tm is not None and abs(terminal_multiple - reg_tm) > 1.0:
            violations.append(
                f"terminal_multiple {terminal_multiple} deviates more than ±1x "
                f"from registered {reg_tm}"
            )

        reg_dr = registry.get("discount_rate")
        if reg_dr is not None and abs(discount_rate - reg_dr) > 1.0:
            violations.append(
                f"discount_rate {discount_rate}% deviates more than ±1pp from "
                f"registered {reg_dr}%"
            )

        reg_period = registry.get("projection_period")
        if reg_period is not None and len(growth_rates) != reg_period:
            violations.append(
                f"projection period {len(growth_rates)} years does not match "
                f"registered {reg_period} years"
            )

        reg_method = registry.get("base_fcf_method")
        if base_fcf_method and reg_method and base_fcf_method != reg_method:
            violations.append(
                f"base_fcf_method '{base_fcf_method}' does not match registered "
                f"'{reg_method}'"
            )

        # Schedule comparison only when the registered schedule is itself
        # well-formed (length == registered period). Malformed legacy
        # schedules are treated as absent rather than blocking the agent.
        reg_schedule = registry.get("growth_schedule")
        if reg_schedule and growth_rates and reg_period is not None:
            if len(reg_schedule) == reg_period:
                if len(reg_schedule) != len(growth_rates):
                    violations.append(
                        f"growth schedule length {len(growth_rates)} does not match "
                        f"registered length {len(reg_schedule)}"
                    )
                else:
                    for i, (proposed, registered) in enumerate(
                        zip(growth_rates, reg_schedule)
                    ):
                        if abs(proposed - registered) > 3.0:
                            violations.append(
                                f"growth year {i + 1} ({proposed}%) deviates more "
                                f"than ±3pp from registered {registered}%"
                            )
                            break
        return violations

    # First run (no registry): absolute guardrails.
    if wacc_baseline is not None:
        if abs(discount_rate - wacc_baseline) > 2.0:
            violations.append(
                f"discount_rate {discount_rate}% deviates more than ±2pp from "
                f"calculated WACC {wacc_baseline}%"
            )
    elif not (9.0 <= discount_rate <= 11.0):
        violations.append(
            f"discount_rate {discount_rate}% outside 9-11% default band "
            "(no WACC baseline available)"
        )

    if not (8.0 <= terminal_multiple <= 16.0):
        violations.append(
            f"terminal_multiple {terminal_multiple} outside 8-16x band for a "
            "first analysis"
        )

    if growth_rates:
        if growth_rates[-1] > 5.0:
            violations.append(
                f"final-year growth {growth_rates[-1]}% exceeds 5% (terminal "
                "growth must converge to maturity)"
            )
        for i in range(1, len(growth_rates)):
            jump = growth_rates[i] - growth_rates[i - 1]
            if jump > 5.0:
                violations.append(
                    f"growth schedule increases {jump:.1f}pp from year {i} to "
                    f"year {i + 1} (schedules must fade, not accelerate)"
                )
                break

    return violations


def calculate_confidence_score(
    growth_rates: List[float],
    terminal_multiple: float,
    discount_rate: float,
    historical_cagr: Optional[float] = None,
    historical_pe_range: Optional[Tuple[float, float]] = None,
    company_revenue: Optional[float] = None,
) -> Dict[str, Any]:
    """Calculate a confidence score for the valuation.

    Higher score = assumptions closer to historical performance = more reliable.
    Lower score = more aggressive/conservative assumptions = less reliable.

    Args:
        growth_rates: List of growth rates used in DCF
        terminal_multiple: Terminal value multiple
        discount_rate: Discount rate as percentage
        historical_cagr: Historical CAGR for comparison
        historical_pe_range: Tuple of (min, max) historical P/E or P/FCF
        company_revenue: Most recent revenue

    Returns:
        Dictionary with confidence_score (0-100), confidence_level, and risk_factors
    """
    score = 100
    risk_factors = []

    # Growth rate deviation from historical
    if historical_cagr and historical_cagr > 0 and growth_rates:
        avg_growth = sum(growth_rates[:5]) / min(5, len(growth_rates))
        growth_deviation = abs(avg_growth - historical_cagr) / historical_cagr

        if growth_deviation > 1.0:  # >100% deviation
            score -= 30
            risk_factors.append(
                f"Growth rate ({avg_growth:.1f}%) deviates {growth_deviation*100:.0f}% from historical ({historical_cagr:.1f}%)"
            )
        elif growth_deviation > 0.5:  # >50% deviation
            score -= 20
            risk_factors.append(
                f"Growth rate ({avg_growth:.1f}%) deviates {growth_deviation*100:.0f}% from historical ({historical_cagr:.1f}%)"
            )
        elif growth_deviation > 0.3:  # >30% deviation
            score -= 10
            risk_factors.append(
                f"Growth rate ({avg_growth:.1f}%) deviates {growth_deviation*100:.0f}% from historical ({historical_cagr:.1f}%)"
            )

    # Terminal multiple deviation from historical
    if historical_pe_range:
        min_pe, max_pe = historical_pe_range
        avg_pe = (min_pe + max_pe) / 2
        pe_deviation = abs(terminal_multiple - avg_pe) / avg_pe

        if pe_deviation > 0.5:
            score -= 20
            risk_factors.append(
                f"Terminal multiple ({terminal_multiple}x) deviates {pe_deviation*100:.0f}% from historical average ({avg_pe:.1f}x)"
            )
        elif pe_deviation > 0.3:
            score -= 10
            risk_factors.append(
                f"Terminal multiple ({terminal_multiple}x) deviates {pe_deviation*100:.0f}% from historical average ({avg_pe:.1f}x)"
            )

    # Terminal multiple absolute range
    if terminal_multiple > 25:
        score -= 15
        risk_factors.append(
            f"Terminal multiple ({terminal_multiple}x) is above typical range (8-25x)"
        )
    elif terminal_multiple < 8:
        score -= 10
        risk_factors.append(
            f"Terminal multiple ({terminal_multiple}x) is below typical range (8-25x)"
        )

    # Discount rate reasonableness
    if discount_rate < 6 or discount_rate > 15:
        score -= 10
        risk_factors.append(
            f"Discount rate ({discount_rate}%) outside standard range (6-15%)"
        )

    # Large company growth check
    if company_revenue and company_revenue > 100_000_000_000:
        max_growth = max(growth_rates[:5]) if growth_rates else 0
        if max_growth > 20:
            score -= 15
            risk_factors.append(
                f"{max_growth}% growth rate is aggressive for ${company_revenue/1e9:.0f}B revenue company"
            )

    # Ensure score doesn't go below 0
    score = max(0, score)

    # Determine confidence level
    if score >= 80:
        confidence_level = "HIGH"
    elif score >= 60:
        confidence_level = "MEDIUM"
    elif score >= 40:
        confidence_level = "LOW"
    else:
        confidence_level = "VERY LOW"

    return {
        "confidence_score": score,
        "confidence_level": confidence_level,
        "risk_factors": risk_factors,
        "interpretation": _get_confidence_interpretation(score, confidence_level),
    }


def _get_confidence_interpretation(score: int, level: str) -> str:
    """Get a human-readable interpretation of the confidence score."""
    if level == "HIGH":
        return (
            "This valuation uses assumptions that are close to historical performance "
            "and within typical ranges. The result is relatively reliable."
        )
    elif level == "MEDIUM":
        return (
            "This valuation uses some assumptions that deviate from historical performance "
            "or typical ranges. Consider the risk factors when making decisions."
        )
    elif level == "LOW":
        return (
            "This valuation uses aggressive or conservative assumptions that significantly "
            "deviate from historical performance. The result should be treated with caution."
        )
    else:
        return (
            "This valuation uses extreme assumptions. The result is highly speculative "
            "and should not be used as a primary decision factor."
        )


def assess_valuation(
    current_price: Optional[float],
    intrinsic_value: Optional[float],
) -> Dict[str, Any]:
    """Assess whether a stock is overvalued, undervalued, or fairly valued.

    Args:
        current_price: Current stock price
        intrinsic_value: Calculated intrinsic value per share

    Returns:
        Dictionary with assessment, ratio, and interpretation
    """
    if not current_price or not intrinsic_value or intrinsic_value <= 0:
        return {
            "assessment": "UNKNOWN",
            "price_to_iv_ratio": None,
            "interpretation": "Unable to assess valuation due to missing data.",
        }

    ratio = current_price / intrinsic_value

    if ratio > 2.0:
        assessment = "EXTREMELY OVERVALUED"
        interpretation = (
            f"Trading at {ratio:.1f}x intrinsic value. "
            "The stock is priced for perfection with no margin of safety."
        )
    elif ratio > 1.5:
        assessment = "SIGNIFICANTLY OVERVALUED"
        interpretation = (
            f"Trading at {ratio:.1f}x intrinsic value. "
            "The stock appears expensive relative to fundamentals."
        )
    elif ratio > 1.2:
        assessment = "OVERVALUED"
        interpretation = (
            f"Trading at {ratio:.1f}x intrinsic value. "
            "The stock is somewhat expensive but may be justified by quality."
        )
    elif ratio > 0.8:
        assessment = "FAIRLY VALUED"
        interpretation = (
            f"Trading at {ratio:.1f}x intrinsic value. "
            "The stock is priced near its estimated worth."
        )
    elif ratio > 0.5:
        assessment = "UNDERVALUED"
        interpretation = (
            f"Trading at {ratio:.1f}x intrinsic value. "
            "The stock appears to offer a margin of safety."
        )
    else:
        assessment = "SIGNIFICANTLY UNDERVALUED"
        interpretation = (
            f"Trading at {ratio:.1f}x intrinsic value. "
            "The stock appears deeply undervalued. Verify assumptions and check for risks."
        )

    return {
        "assessment": assessment,
        "price_to_iv_ratio": round(ratio, 2),
        "interpretation": interpretation,
    }
