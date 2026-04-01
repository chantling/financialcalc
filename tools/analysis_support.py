"""Analysis support tools"""

import itertools
import logging
from typing import Any, Dict, List, Optional

from financialcalc.utils.error_handling import CalculationError, ValidationError

logger = logging.getLogger(__name__)


def calculate_margin_of_safety(intrinsic_value: float, margin_percent: float) -> Dict:
    """Calculate buy price with margin of safety

    Args:
        intrinsic_value: Calculated intrinsic value per share
        margin_percent: Margin of safety percentage (0-100)

    Returns:
        Dictionary with buy_price and margin_amount
    """
    if intrinsic_value <= 0:
        raise ValidationError("Intrinsic value must be positive")

    if margin_percent < 0 or margin_percent > 100:
        raise ValidationError("Margin percent must be between 0 and 100")

    buy_price = intrinsic_value * (1 - margin_percent / 100)
    margin_amount = intrinsic_value - buy_price

    return {
        "buy_price": round(buy_price, 2),
        "margin_amount": round(margin_amount, 2),
        "intrinsic_value": round(intrinsic_value, 2),
        "margin_percent": margin_percent,
    }


def calculate_sensitivity_analysis(
    base_value: float,
    variables: List[str],
    ranges: List[List[float]],
    dcf_params: Optional[Dict[str, Any]] = None,
) -> Dict:
    """Perform sensitivity analysis on key variables.

    Two modes:
    1. Simple mode (no dcf_params): Applies percentage adjustments to base_value
    2. DCF mode (with dcf_params): Recalculates full DCF for each scenario

    Args:
        base_value: Base intrinsic value
        variables: Array of variable names (e.g., ['growth_rate', 'discount_rate'])
        ranges: Array of value ranges for each variable
        dcf_params: Optional dict with DCF parameters for recalculated analysis:
            - base_fcf: Base year FCF
            - growth_rates: Base growth rate array
            - discount_rate: Base discount rate
            - terminal_multiple: Base terminal multiple
            - shares_outstanding: Shares outstanding
            - net_cash: Net cash/debt

    Returns:
        Dictionary with sensitivity_matrix, best_case, worst_case
    """
    if base_value <= 0:
        raise ValidationError("Base value must be positive")

    if len(variables) != len(ranges):
        raise ValidationError("Variables and ranges arrays must match in length")

    if len(variables) > 3:
        raise ValidationError("Maximum 3 variables for sensitivity analysis")

    if dcf_params:
        return _dcf_sensitivity_analysis(base_value, variables, ranges, dcf_params)

    # Simple percentage-based sensitivity (legacy mode)
    combinations = list(itertools.product(*ranges))

    sensitivity_matrix = []
    for combo in combinations:
        adjustment = 1.0
        for i, val in enumerate(combo):
            adjustment *= 1 + val / 100

        adjusted_value = base_value * adjustment
        sensitivity_matrix.append(
            {
                "variables": dict(zip(variables, combo)),
                "adjusted_value": round(adjusted_value, 2),
                "change_percent": round(
                    (adjusted_value - base_value) / base_value * 100, 2
                ),
            }
        )

    sorted_values = sorted(sensitivity_matrix, key=lambda x: x["adjusted_value"])

    return {
        "base_value": round(base_value, 2),
        "variables": variables,
        "sensitivity_matrix": sensitivity_matrix,
        "best_case": sorted_values[-1],
        "worst_case": sorted_values[0],
        "total_scenarios": len(sensitivity_matrix),
    }


def _dcf_sensitivity_analysis(
    base_value: float,
    variables: List[str],
    ranges: List[List[float]],
    dcf_params: Dict[str, Any],
) -> Dict:
    """Recalculate full DCF for each sensitivity scenario."""
    from financialcalc.tools.core_calculations import (
        calculate_intrinsic_value,
        calculate_present_value,
        calculate_terminal_value,
        project_cash_flows,
    )

    base_fcf = dcf_params["base_fcf"]
    base_growth_rates = dcf_params["growth_rates"]
    base_discount_rate = dcf_params["discount_rate"]
    base_terminal_multiple = dcf_params["terminal_multiple"]
    shares_outstanding = dcf_params["shares_outstanding"]
    net_cash = dcf_params.get("net_cash", 0)
    years = len(base_growth_rates)

    combinations = list(itertools.product(*ranges))
    sensitivity_matrix = []

    for combo in combinations:
        # Start with base parameters
        growth_rates = list(base_growth_rates)
        discount_rate = base_discount_rate
        terminal_multiple = base_terminal_multiple

        # Apply variable adjustments
        for i, (var_name, val) in enumerate(zip(variables, combo)):
            if var_name in ("growth_rate", "growth"):
                # val is the growth rate to use for all years
                growth_rates = [val] * years
            elif var_name in ("yr1_5_growth",):
                # Apply to first half
                half = years // 2
                growth_rates = [val] * half + growth_rates[half:]
            elif var_name in ("yr6_10_growth",):
                # Apply to second half
                half = years // 2
                growth_rates = growth_rates[:half] + [val] * (years - half)
            elif var_name in ("discount_rate",):
                discount_rate = val
            elif var_name in ("terminal_multiple",):
                terminal_multiple = val

        # Run DCF calculation
        try:
            if discount_rate <= 0:
                raise CalculationError(
                    f"Discount rate must be positive, got {discount_rate}%"
                )
            proj = project_cash_flows(base_fcf, growth_rates, years)
            pv = calculate_present_value(proj["projected_flows"], discount_rate)
            tv = calculate_terminal_value(
                proj["projected_flows"][-1], terminal_multiple
            )
            # Discount terminal value back to present by the full projection period
            r = discount_rate / 100
            tv_pv_amount = tv["terminal_value"] / (1 + r) ** years
            iv = calculate_intrinsic_value(
                pv["total_pv"], tv_pv_amount, net_cash, shares_outstanding
            )
            adjusted_value = iv["intrinsic_value"]
        except Exception as e:
            logger.warning(
                f"DCF sensitivity scenario failed for {dict(zip(variables, combo))}: {e}"
            )
            adjusted_value = 0

        sensitivity_matrix.append(
            {
                "variables": dict(zip(variables, combo)),
                "adjusted_value": round(adjusted_value, 2),
                "change_percent": (
                    round((adjusted_value - base_value) / base_value * 100, 2)
                    if base_value != 0
                    else 0
                ),
            }
        )

    sorted_values = sorted(sensitivity_matrix, key=lambda x: x["adjusted_value"])

    return {
        "base_value": round(base_value, 2),
        "variables": variables,
        "sensitivity_matrix": sensitivity_matrix,
        "best_case": sorted_values[-1] if sorted_values else None,
        "worst_case": sorted_values[0] if sorted_values else None,
        "total_scenarios": len(sensitivity_matrix),
        "mode": "dcf_recalculation",
    }


def calculate_probability_weighted(
    values: List[float], probabilities: List[float]
) -> Dict:
    """Calculate probability-weighted average of scenarios

    Args:
        values: Array of scenario values
        probabilities: Array of probabilities (must sum to 100%)

    Returns:
        Dictionary with weighted_average and scenario_breakdown
    """
    if len(values) != len(probabilities):
        raise ValidationError("Values and probabilities arrays must match in length")

    if not probabilities:
        raise ValidationError("Probabilities array cannot be empty")

    total_prob = sum(probabilities)
    if abs(total_prob - 100) > 0.01:
        raise ValidationError(f"Probabilities must sum to 100%, got {total_prob}%")

    if any(p <= 0 for p in probabilities):
        raise ValidationError("All probabilities must be positive")

    weighted_average = sum(v * p / 100 for v, p in zip(values, probabilities))

    scenario_breakdown = [
        {
            "value": round(v, 2),
            "probability": p,
            "contribution": round(v * p / 100, 2),
        }
        for v, p in zip(values, probabilities)
    ]

    return {
        "weighted_average": round(weighted_average, 2),
        "scenario_breakdown": scenario_breakdown,
    }


def calculate_sotp_valuation(
    components: List[Dict[str, Any]],
    overlap_adjustment_percent: float = 0,
) -> Dict:
    """Calculate Sum-of-the-Parts (SOTP) intrinsic value.

    Used for conglomerates and holding companies where different business
    segments or asset classes should be valued separately.

    Args:
        components: List of dicts with 'name' (str) and 'value_per_share' (float)
        overlap_adjustment_percent: Percentage to subtract for overlap/double-counting
                                    (e.g., 15 for 15% reduction)

    Returns:
        Dictionary with intrinsic_value, adjusted_value, component_breakdown
    """
    if not components:
        raise ValidationError("Components array cannot be empty")

    if overlap_adjustment_percent < 0 or overlap_adjustment_percent > 100:
        raise ValidationError("Overlap adjustment must be between 0 and 100")

    total_value = 0
    component_details = []

    for comp in components:
        name = comp.get("name", "unnamed")
        value = comp.get("value_per_share", 0)
        if value < 0:
            raise ValidationError(
                f"Component '{name}' has negative value_per_share: {value}"
            )
        total_value += value
        component_details.append({"name": name, "value_per_share": round(value, 2)})

    adjusted_value = total_value * (1 - overlap_adjustment_percent / 100)

    return {
        "intrinsic_value": round(total_value, 2),
        "adjusted_value": round(adjusted_value, 2),
        "overlap_adjustment_percent": overlap_adjustment_percent,
        "overlap_deduction": round(total_value - adjusted_value, 2),
        "component_breakdown": component_details,
        "formula": (
            f"Sum({[c['name'] for c in components]})"
            f" x (1 - {overlap_adjustment_percent}%)"
        ),
    }


def normalize_base_value(
    values: List[Optional[float]],
    method: str = "average",
) -> Dict:
    """Normalize a series of values to select a representative base value.

    Useful for selecting a normalized base FCF when the most recent year
    may be an outlier.

    Args:
        values: Array of numerical values (may contain None)
        method: One of 'average', 'median', 'most_recent', 'exclude_outliers'

    Returns:
        Dictionary with normalized_value, method_used, values_used, values_excluded
    """
    if not values:
        raise ValidationError("Values array cannot be empty")

    if method not in ("average", "median", "most_recent", "exclude_outliers"):
        raise ValidationError(
            "Method must be 'average', 'median', 'most_recent', or 'exclude_outliers'"
        )

    # Filter out None values
    valid_values = [v for v in values if v is not None]

    if not valid_values:
        raise ValidationError("No valid values to normalize")

    values_excluded = len(values) - len(valid_values)

    if method == "most_recent":
        # Return the last non-None value
        for v in reversed(values):
            if v is not None:
                return {
                    "normalized_value": round(v, 2),
                    "method_used": method,
                    "values_used": [round(v, 2)],
                    "values_excluded": values_excluded,
                }

    elif method == "average":
        avg = sum(valid_values) / len(valid_values)
        return {
            "normalized_value": round(avg, 2),
            "method_used": method,
            "values_used": [round(v, 2) for v in valid_values],
            "values_excluded": values_excluded,
        }

    elif method == "median":
        sorted_vals = sorted(valid_values)
        n = len(sorted_vals)
        if n % 2 == 0:
            median = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        else:
            median = sorted_vals[n // 2]
        return {
            "normalized_value": round(median, 2),
            "method_used": method,
            "values_used": [round(v, 2) for v in valid_values],
            "values_excluded": values_excluded,
        }

    elif method == "exclude_outliers":
        if len(valid_values) < 3:
            # Not enough data to exclude outliers, use average
            avg = sum(valid_values) / len(valid_values)
            return {
                "normalized_value": round(avg, 2),
                "method_used": "average (insufficient data for outlier exclusion)",
                "values_used": [round(v, 2) for v in valid_values],
                "values_excluded": values_excluded,
            }

        sorted_vals = sorted(valid_values)
        # Remove top and bottom values
        trimmed = sorted_vals[1:-1]
        avg = sum(trimmed) / len(trimmed)
        excluded = [sorted_vals[0], sorted_vals[-1]]
        return {
            "normalized_value": round(avg, 2),
            "method_used": method,
            "values_used": [round(v, 2) for v in trimmed],
            "values_excluded": values_excluded + len(excluded),
            "outliers_excluded": [round(v, 2) for v in excluded],
        }

    # Fallback (should not reach here)
    raise CalculationError(f"Unknown normalization method: {method}")
