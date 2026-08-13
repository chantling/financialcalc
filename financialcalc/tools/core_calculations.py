"""Core financial calculation tools"""

import math
from typing import Any, Dict, List, Optional

from financialcalc.utils.error_handling import CalculationError, ValidationError


def calculate_cagr(values: List[Optional[float]], years: Optional[int] = None) -> Dict:
    """Calculate Compound Annual Growth Rate

    Formula: CAGR = (Final/Initial)^(1/Years) - 1
    Years is automatically calculated as time span between first and last values.

    Args:
        values: Array of numerical values or None (at least 2 valid values)
        years: Ignored (calculated automatically)

    Returns:
        Dictionary with cagr and detailed_calculation (includes calculated years)
    """
    filtered_values = [
        v
        for v in values
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ]

    if len(filtered_values) < 2:
        raise ValidationError("Values array must have at least 2 valid values")

    years = len(filtered_values) - 1

    if years <= 0:
        raise ValidationError("Years must be a positive integer")

    initial = filtered_values[0]
    final = filtered_values[-1]

    if initial <= 0:
        raise ValidationError(
            f"Initial value must be positive for CAGR calculation, got {initial}"
        )

    if final < 0:
        raise ValidationError(
            f"Final value must be non-negative for CAGR calculation, got {final}. "
            "CAGR is undefined when the sign changes from positive to negative."
        )

    try:
        cagr = (final / initial) ** (1 / years) - 1
    except ZeroDivisionError:
        raise CalculationError("Cannot calculate CAGR with zero years")

    cagr = round(cagr * 100, 4)

    return {
        "cagr": cagr,
        "detailed_calculation": {
            "initial_value": initial,
            "final_value": final,
            "years": years,
            "formula": f"( {final} / {initial} ) ^ (1/{years}) - 1",
            "result_percentage": f"{cagr:.4f}%",
        },
    }


def project_cash_flows(
    base_fcf: float, growth_rates: List[float], years: int = 10
) -> Dict:
    """Project future cash flows with custom growth rates

    Args:
        base_fcf: Base year free cash flow (must be non-negative)
        growth_rates: Array of growth rates for each year (as percentages)
        years: Number of projection years

    Returns:
        Dictionary with projected_flows and growth_schedule
    """
    if base_fcf < 0:
        raise ValidationError("Base FCF must be non-negative")

    if len(growth_rates) != years:
        raise ValidationError("Growth rates array length must match years")

    if any(r < -100 for r in growth_rates):
        raise ValidationError("Growth rates must be >= -100% (cannot shrink to zero)")

    projected_flows = []
    current_fcf = base_fcf

    for rate in growth_rates:
        current_fcf = current_fcf * (1 + rate / 100)
        projected_flows.append(current_fcf)

    return {
        "projected_flows": [round(f, 2) for f in projected_flows],
        "growth_schedule": [round(r, 2) for r in growth_rates],
        "base_fcf": base_fcf,
        "projection_years": years,
    }


def calculate_present_value(cash_flows: List[float], discount_rate: float) -> Dict:
    """Calculate present value of cash flow series

    Args:
        cash_flows: Array of future cash flows
        discount_rate: Discount rate as percentage

    Returns:
        Dictionary with present_values, total_pv, pv_factors
    """
    if not cash_flows:
        raise ValidationError("Cash flows array cannot be empty")

    if discount_rate <= 0:
        raise ValidationError("Discount rate must be positive")

    if discount_rate > 50:
        raise ValidationError("Discount rate should not exceed 50%")

    import numpy as np

    r = discount_rate / 100
    periods = np.arange(1, len(cash_flows) + 1)
    pv_factors = 1 / (1 + r) ** periods

    present_values = cash_flows * pv_factors
    total_pv = np.sum(present_values)

    return {
        "present_values": [round(float(pv), 2) for pv in present_values],
        "total_pv": round(float(total_pv), 2),
        "pv_factors": [round(float(pvf), 6) for pvf in pv_factors],
        "discount_rate": discount_rate,
    }


def calculate_terminal_value(final_year_fcf: float, terminal_multiple: float) -> Dict:
    """Calculate terminal value using multiple method

    Args:
        final_year_fcf: Final year projected free cash flow
        terminal_multiple: Terminal multiple (e.g., 15 for 15x)

    Returns:
        Dictionary with terminal_value and calculation_details
    """
    if final_year_fcf <= 0:
        raise ValidationError("Final year FCF must be positive")

    if terminal_multiple <= 0:
        raise ValidationError("Terminal multiple must be positive")

    if terminal_multiple > 100:
        raise ValidationError("Terminal multiple should be reasonable (<100)")

    terminal_value = final_year_fcf * terminal_multiple

    return {
        "terminal_value": round(terminal_value, 2),
        "calculation_details": {
            "final_year_fcf": round(final_year_fcf, 2),
            "terminal_multiple": terminal_multiple,
            "formula": f"{round(final_year_fcf, 2)} x {terminal_multiple}",
        },
    }


def calculate_intrinsic_value(
    pv_cash_flows: float,
    pv_terminal_value: float,
    net_cash: float,
    shares_outstanding: float,
) -> Dict:
    """Calculate per-share intrinsic value

    Formula: IV = (PV Flows + PV Terminal + Net Cash) / Shares

    Args:
        pv_cash_flows: Present value of projected cash flows
        pv_terminal_value: Present value of terminal value
        net_cash: Net cash/debt position (positive = net cash, negative = net debt)
        shares_outstanding: Number of shares outstanding

    Returns:
        Dictionary with intrinsic_value, enterprise_value, calculation_breakdown
    """
    if shares_outstanding <= 0:
        raise ValidationError("Shares outstanding must be positive")

    enterprise_value = pv_cash_flows + pv_terminal_value + net_cash
    intrinsic_value = enterprise_value / shares_outstanding

    pv_cash_rounded = round(pv_cash_flows, 2)
    pv_terminal_rounded = round(pv_terminal_value, 2)
    net_cash_rounded = round(net_cash, 2)
    formula_str = (
        f"({pv_cash_rounded} + {pv_terminal_rounded} + {net_cash_rounded}) / "
        f"{shares_outstanding}"
    )
    return {
        "intrinsic_value": round(intrinsic_value, 2),
        "enterprise_value": round(enterprise_value, 2),
        "calculation_breakdown": {
            "pv_cash_flows": round(pv_cash_flows, 2),
            "pv_terminal_value": round(pv_terminal_value, 2),
            "net_cash": round(net_cash, 2),
            "enterprise_value": round(enterprise_value, 2),
            "shares_outstanding": shares_outstanding,
            "formula": formula_str,
        },
    }


def calculate_net_debt(
    total_cash: Optional[float] = None,
    total_debt: Optional[float] = None,
    cash_and_equivalents: Optional[float] = None,
    short_term_investments: Optional[float] = None,
    short_term_debt: Optional[float] = None,
    long_term_debt: Optional[float] = None,
) -> Dict:
    """Calculate net cash/debt position.

    Can be called with either:
    - total_cash and total_debt (simplified)
    - cash_and_equivalents, short_term_investments, short_term_debt,
      long_term_debt (detailed)

    Args:
        total_cash: Total cash position (cash + short-term investments)
        total_debt: Total debt (short-term + long-term)
        cash_and_equivalents: Cash and cash equivalents
        short_term_investments: Short-term investments
        short_term_debt: Short-term debt
        long_term_debt: Long-term debt

    Returns:
        Dictionary with net_cash (positive = net cash, negative = net debt),
        total_cash, total_debt, and calculation_breakdown
    """
    # Calculate total cash
    if total_cash is None:
        c = 0 if cash_and_equivalents is None else cash_and_equivalents
        s = 0 if short_term_investments is None else short_term_investments
        calculated_cash = c + s
    else:
        calculated_cash = total_cash

    # Calculate total debt
    if total_debt is None:
        st = 0 if short_term_debt is None else short_term_debt
        lt = 0 if long_term_debt is None else long_term_debt
        calculated_debt = st + lt
    else:
        calculated_debt = total_debt

    net_cash = calculated_cash - calculated_debt

    return {
        "net_cash": round(net_cash, 2),
        "total_cash": round(calculated_cash, 2),
        "total_debt": round(calculated_debt, 2),
        "calculation_breakdown": {
            "cash_and_equivalents": round(
                0 if cash_and_equivalents is None else cash_and_equivalents, 2
            ),
            "short_term_investments": round(
                0 if short_term_investments is None else short_term_investments, 2
            ),
            "short_term_debt": round(
                0 if short_term_debt is None else short_term_debt, 2
            ),
            "long_term_debt": round(
                0 if long_term_debt is None else long_term_debt, 2
            ),
            "formula": f"({round(calculated_cash, 2)} - {round(calculated_debt, 2)})",
            "interpretation": "Net Cash" if net_cash >= 0 else "Net Debt",
        },
    }


def calculate_graham_formula(eps: float, growth_rate: float, bond_yield: float) -> Dict:
    """Calculate intrinsic value using Benjamin Graham's formula.

    Formula: IV = EPS x [8.5 + (2 x G)] x 4.4 / Y

    Args:
        eps: Earnings per share
        growth_rate: Expected growth rate as percentage (e.g., 7 for 7%)
        bond_yield: Current AAA corporate bond yield as percentage (e.g., 5 for 5%)

    Returns:
        Dictionary with intrinsic_value and formula_breakdown
    """
    if eps <= 0:
        raise ValidationError("EPS must be positive")

    if bond_yield <= 0:
        raise ValidationError("Bond yield must be positive")

    multiplier = 8.5 + (2 * growth_rate)
    intrinsic_value = eps * multiplier * 4.4 / bond_yield

    return {
        "intrinsic_value": round(intrinsic_value, 2),
        "formula_breakdown": {
            "eps": eps,
            "growth_rate": growth_rate,
            "bond_yield": bond_yield,
            "multiplier": round(multiplier, 2),
            "formula": (
                f"{eps} x [8.5 + (2 x {growth_rate})]" f" x 4.4 / {bond_yield}"
            ),
            "intermediate": (
                f"{eps} x {round(multiplier, 2)}" f" x {round(4.4 / bond_yield, 4)}"
            ),
        },
    }


def calculate_custom_fcf(
    operating_cash_flow: float,
    capital_expenditures: float,
    adjustments: Optional[List[Dict[str, Any]]] = None,
) -> Dict:
    """Calculate adjusted free cash flow with custom adjustments.

    Base FCF = Operating Cash Flow - Capital Expenditures
    Adjusted FCF = Base FCF + sum(adjustments)

    Args:
        operating_cash_flow: Operating cash flow
        capital_expenditures: Capital expenditures (positive number, will be subtracted)
        adjustments: Optional list of {name: str, value: float} adjustments
                     Positive values add to FCF, negative values subtract.
                     Example: [{"name": "SBC", "value": -12000000000}]

    Returns:
        Dictionary with base_fcf, adjusted_fcf, and breakdown
    """
    base_fcf = operating_cash_flow - capital_expenditures

    total_adjustments = 0
    adjustment_details = []

    if adjustments:
        for adj in adjustments:
            name = adj.get("name", "unnamed")
            value = adj.get("value", 0)
            total_adjustments += value
            adjustment_details.append({"name": name, "value": round(value, 2)})

    adjusted_fcf = base_fcf + total_adjustments

    return {
        "base_fcf": round(base_fcf, 2),
        "adjusted_fcf": round(adjusted_fcf, 2),
        "total_adjustments": round(total_adjustments, 2),
        "adjustment_details": adjustment_details,
        "calculation_breakdown": {
            "operating_cash_flow": round(operating_cash_flow, 2),
            "capital_expenditures": round(capital_expenditures, 2),
            "formula": (
                f"({round(operating_cash_flow, 2)}"
                f" - {round(capital_expenditures, 2)})"
                f" + {round(total_adjustments, 2)}"
            ),
        },
    }
