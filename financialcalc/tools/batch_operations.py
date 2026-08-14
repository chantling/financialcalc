"""Batch operations for complete workflows"""

from financialcalc.tools.analysis_support import (
    calculate_margin_of_safety,
    calculate_sensitivity_analysis,
)
from financialcalc.tools.core_calculations import (
    calculate_cagr,
    calculate_intrinsic_value,
    calculate_present_value,
    calculate_terminal_value,
    project_cash_flows,
)
from financialcalc.utils.error_handling import ValidationError


def run_complete_dcf_analysis(financial_data: dict, assumptions: dict) -> dict:
    """Execute complete DCF analysis with user-supplied assumptions.

    Redesigned interface: The LLM agent supplies ALL key parameters.
    This tool performs the calculations accurately.

    Args:
        financial_data: Retrieved financial data (from get_financial_data).
            Used for historical CAGR calculation and reference only.
        assumptions: Dictionary containing:
            - base_fcf (float, required): User-selected base year FCF
            - growth_rates (List[float], required): Array of growth rates per year
            - discount_rate (float, required): Discount rate as percentage
            - terminal_multiple (float, required): Terminal value multiple
            - margin_of_safety (float, required): Margin of safety percentage
            - net_cash (float, optional): Net cash/debt position (default: 0)
            - shares_outstanding (float, optional): Override shares from
              financial_data. When not provided, diluted_shares_outstanding
              is preferred over basic shares_outstanding per methodology.

    Returns:
        Complete analysis results including projected_flows, pv, tv, iv,
        buy_price, sensitivity_analysis, and calculation_log for verification.
    """
    required_assumptions = [
        "base_fcf",
        "growth_rates",
        "discount_rate",
        "terminal_multiple",
        "margin_of_safety",
    ]
    for key in required_assumptions:
        if key not in assumptions:
            raise ValidationError(f"Missing required assumption: {key}")

    base_fcf = assumptions["base_fcf"]
    growth_rates = assumptions["growth_rates"]
    discount_rate = assumptions["discount_rate"]
    terminal_multiple = assumptions["terminal_multiple"]
    margin_of_safety = assumptions["margin_of_safety"]
    net_cash = assumptions.get("net_cash", 0)

    # Get shares outstanding from assumptions or financial_data
    # Prefer diluted (per IV_Distilled.md methodology); fall back to basic.
    shares_outstanding = assumptions.get("shares_outstanding")
    if not shares_outstanding or shares_outstanding <= 0:
        shares_outstanding = financial_data.get("diluted_shares_outstanding")
    if not shares_outstanding or shares_outstanding <= 0:
        shares_outstanding = financial_data.get("shares_outstanding")
    if not shares_outstanding or shares_outstanding <= 0:
        raise ValidationError("shares_outstanding must be provided and positive")

    years = len(growth_rates)

    # Build calculation log for verification
    calculation_log = []

    # Step 1: Historical CAGR (for reference)
    cagr_result = None
    if "free_cash_flow" in financial_data and financial_data["free_cash_flow"]:
        try:
            cagr_result = calculate_cagr(financial_data["free_cash_flow"])
            calculation_log.append(f"Historical FCF CAGR: {cagr_result['cagr']}%")
        except Exception:
            calculation_log.append("Historical FCF CAGR: Could not calculate")

    # Step 2: Project cash flows
    projection_result = project_cash_flows(base_fcf, growth_rates, years)
    calculation_log.append(
        f"Projected {years} years of cash flows from base FCF of {base_fcf}"
    )
    calculation_log.append(
        f"Year 1 FCF: {projection_result['projected_flows'][0]}, "
        f"Year {years} FCF: {projection_result['projected_flows'][-1]}"
    )

    # Step 3: Present value of projected cash flows
    pv_result = calculate_present_value(
        projection_result["projected_flows"], discount_rate
    )
    calculation_log.append(
        f"PV of projected cash flows at {discount_rate}%: {pv_result['total_pv']}"
    )

    # Step 4: Terminal value
    final_year_fcf = projection_result["projected_flows"][-1]
    tv_result = calculate_terminal_value(final_year_fcf, terminal_multiple)
    calculation_log.append(
        f"Terminal value: {final_year_fcf} x {terminal_multiple} = "
        f"{tv_result['terminal_value']}"
    )

    # Step 5: PV of terminal value
    # Discount terminal value back to present by the full projection period
    r = discount_rate / 100
    tv_pv_amount = tv_result["terminal_value"] / (1 + r) ** years
    calculation_log.append(
        f"PV of terminal value at {discount_rate}% over {years} years: {round(tv_pv_amount, 2)}"
    )

    # Step 6: Intrinsic value per share
    iv_result = calculate_intrinsic_value(
        pv_result["total_pv"], tv_pv_amount, net_cash, shares_outstanding
    )
    calculation_log.append(
        f"Enterprise value: {iv_result['enterprise_value']}, "
        f"Net cash/debt: {net_cash}"
    )
    calculation_log.append(f"Intrinsic value per share: {iv_result['intrinsic_value']}")

    # Step 7: Margin of safety
    mos_result = calculate_margin_of_safety(
        iv_result["intrinsic_value"], margin_of_safety
    )
    calculation_log.append(
        f"Buy price ({margin_of_safety}% MoS): {mos_result['buy_price']}"
    )

    # Step 8: Sensitivity analysis (with DCF recalculation)
    sensitivity_result = calculate_sensitivity_analysis(
        iv_result["intrinsic_value"],
        ["growth_rate", "discount_rate"],
        [[-5, 0, 5], [-2, 0, 2]],
        dcf_params={
            "base_fcf": base_fcf,
            "growth_rates": growth_rates,
            "discount_rate": discount_rate,
            "terminal_multiple": terminal_multiple,
            "shares_outstanding": shares_outstanding,
            "net_cash": net_cash,
        },
    )

    result = {
        "symbol": financial_data.get("symbol", "UNKNOWN"),
        "assumptions": {
            "base_fcf": base_fcf,
            "growth_rates": growth_rates,
            "discount_rate": discount_rate,
            "terminal_multiple": terminal_multiple,
            "margin_of_safety": margin_of_safety,
            "net_cash": net_cash,
            "shares_outstanding": shares_outstanding,
        },
        "historical_cagr": cagr_result["cagr"] if cagr_result else None,
        "projected_cash_flows": projection_result["projected_flows"],
        "present_value_flows": pv_result["total_pv"],
        "terminal_value": tv_result["terminal_value"],
        "present_value_terminal": round(tv_pv_amount, 2),
        "enterprise_value": iv_result["enterprise_value"],
        "intrinsic_value": iv_result["intrinsic_value"],
        "buy_price": mos_result["buy_price"],
        "margin_of_safety": margin_of_safety,
        "sensitivity_analysis": sensitivity_result,
        "calculation_log": calculation_log,
    }

    if "dates" in financial_data:
        result["dates"] = financial_data["dates"]

    return result
