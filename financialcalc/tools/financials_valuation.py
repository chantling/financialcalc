"""Valuation math for financial institutions (banks, insurers, etc.).

FCF is structurally meaningless for financial companies: float (premiums
held before claims are paid) distorts operating cash flow, capex is
minimal, and policyholder reserves make debt operational rather than
financing. The standard frameworks are instead:

- Residual Income (excess return on equity): IV = book value + PV of
  future earnings above the cost of equity (Damodaran's recommended
  approach for financial service firms)
- Justified P/B: fair P/B = (ROE - g) / (cost of equity - g)
- Dividend Discount Model: valid because insurers/banks pay out most
  earnings and retain capital only to regulatory need

All rates are expressed as percentages (e.g., 10.5 means 10.5%) except
the resulting multiples and per-share values.
"""

from typing import Any, Dict, List, Optional

from financialcalc.utils.config import settings
from financialcalc.utils.error_handling import ValidationError

# Yahoo sectors for which equity-based valuation replaces FCF DCF.
FINANCIAL_SECTORS = {"Financial Services"}

VALID_ROE_METHODS = ("avg_equity", "beginning_equity")


def is_financial_company(data: Optional[Dict[str, Any]]) -> bool:
    """Check whether a data dict represents a financial company.

    Accepts either a session dict, a financial_data dict (with nested
    company_info), or any dict with a direct "sector" key.

    Args:
        data: Session, financial data, or current metrics dict

    Returns:
        True if the sector is a financial sector
    """
    if not data:
        return False
    company_info = data.get("company_info") or {}
    sector = company_info.get("sector") or data.get("sector")
    return bool(sector and sector in FINANCIAL_SECTORS)


def _extract_sector(*sources: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the first sector string found across candidate dicts."""
    for source in sources:
        if not source:
            continue
        info = source.get("company_info") or {}
        sector = info.get("sector") or source.get("sector")
        if sector:
            return str(sector)
    return None


def classify_valuation_track(
    financial_data: Optional[Dict[str, Any]],
    balance_sheet: Optional[Dict[str, Any]] = None,
    current_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify a ticker as "financial" or "operating" for valuation.

    Sector-first with an asset-light structural override. A provider-
    classified financial company whose market data shows BOTH a very high
    current P/B and a very high average historical ROE (e.g., payment
    networks whose book equity is a buyback residual rather than an
    operational requirement) is routed to the operating-company track,
    because a book-value-anchored residual-income model structurally
    cannot capture its earnings power. When metrics are unavailable the
    classification falls back to the provider sector alone.

    Args:
        financial_data: financial_data dict (company_info.sector and
            net_income history)
        balance_sheet: session balance sheet (total_equity history)
        current_metrics: session current metrics (current_price and share
            count for the P/B estimate)

    Returns:
        Dictionary with:
            - track: "financial" | "operating"
            - asset_light_override: True when the structural override fired
            - sector, current_pb, average_roe, thresholds
            - reason: human-readable explanation of the decision
    """
    sector = _extract_sector(financial_data, current_metrics)
    result: Dict[str, Any] = {
        "sector": sector,
        "track": "operating",
        "asset_light_override": False,
        "current_pb": None,
        "average_roe": None,
        "thresholds": {
            "pb_min": settings.asset_light_pb_min,
            "roe_min": settings.asset_light_roe_min,
        },
        "reason": "",
    }

    if not sector or sector not in FINANCIAL_SECTORS:
        result["reason"] = (
            f"Sector '{sector}' is not a financial sector; operating-company "
            "(FCF DCF) valuation applies."
        )
        return result

    equity_arr = list((balance_sheet or {}).get("total_equity") or [])
    net_income_arr = list((financial_data or {}).get("net_income") or [])

    average_roe = None
    if equity_arr and net_income_arr:
        try:
            roe_result = calculate_roe_series(net_income_arr, equity_arr)
            average_roe = roe_result.get("average_roe")
        except ValidationError:
            average_roe = None
    result["average_roe"] = average_roe

    latest_equity = next((v for v in reversed(equity_arr) if v), None)
    shares = None
    for source in (current_metrics, financial_data):
        if not source:
            continue
        shares = source.get("diluted_shares_outstanding") or source.get(
            "shares_outstanding"
        )
        if shares:
            break
    price = (current_metrics or {}).get("current_price")

    current_pb = None
    if (
        latest_equity
        and latest_equity > 0
        and shares
        and shares > 0
        and price
        and price > 0
    ):
        current_pb = round(price / (latest_equity / shares), 2)
    result["current_pb"] = current_pb

    met: List[str] = []
    if current_pb is not None and current_pb >= settings.asset_light_pb_min:
        met.append(f"P/B {current_pb}x >= {settings.asset_light_pb_min}x")
    if average_roe is not None and average_roe >= settings.asset_light_roe_min:
        met.append(f"avg ROE {average_roe}% >= {settings.asset_light_roe_min}%")

    if len(met) == 2:
        result["track"] = "operating"
        result["asset_light_override"] = True
        result["reason"] = (
            "Provider sector is Financial Services, but the asset-light "
            f"profile applies ({'; '.join(met)}): book equity is a "
            "capital-return residual rather than an operational asset, so "
            "a book-value-anchored residual-income model cannot capture "
            "earnings power. Use the operating-company (FCF DCF) track."
        )
        return result

    result["track"] = "financial"
    if met:
        result["reason"] = (
            "Financial sector with balance-sheet economics "
            f"({'; '.join(met)} but both thresholds are required); "
            "residual-income valuation applies."
        )
    else:
        result["reason"] = (
            "Financial sector; residual-income valuation applies (P/B and "
            "ROE history unavailable or below override thresholds)."
        )
    return result


def calculate_roe_series(
    net_income: List[Optional[float]],
    total_equity: List[Optional[float]],
    method: str = "avg_equity",
) -> Dict[str, Any]:
    """Calculate historical ROE series from net income and equity.

    Formula (avg_equity): ROE_t = NI_t / avg(BV_{t-1}, BV_t) x 100
    Formula (beginning_equity): ROE_t = NI_t / BV_{t-1} x 100

    Arrays are oldest-first (server convention). The first year has no
    prior book value under beginning_equity and is skipped there.

    Args:
        net_income: Net income series (oldest first)
        total_equity: Total equity series (oldest first)
        method: "avg_equity" or "beginning_equity"

    Returns:
        Dictionary with roe_series, dates_aligned, method, years_used

    Raises:
        ValidationError: If method unknown or inputs insufficient
    """
    if method not in VALID_ROE_METHODS:
        raise ValidationError(f"Unknown ROE method: {method}")

    n = min(len(net_income), len(total_equity))
    if n < 1:
        raise ValidationError("Need at least 1 year of net income and equity")

    ni = net_income[-n:]
    eq = total_equity[-n:]

    roe_series: List[Optional[float]] = []
    for i in range(n):
        ni_t = ni[i]
        eq_t = eq[i]
        if ni_t is None or eq_t is None:
            roe_series.append(None)
            continue
        if i == 0:
            if method == "beginning_equity":
                roe_series.append(None)
                continue
            base = eq_t
        else:
            prev = eq[i - 1]
            if prev is None:
                roe_series.append(None)
                continue
            base = (prev + eq_t) / 2 if method == "avg_equity" else prev
        if base is None or base <= 0:
            roe_series.append(None)
            continue
        roe_series.append(round(ni_t / base * 100, 2))

    valid = [r for r in roe_series if r is not None]
    return {
        "roe_series": roe_series,
        "method": method,
        "years_used": len(valid),
        "average_roe": round(sum(valid) / len(valid), 2) if valid else None,
        "latest_roe": valid[-1] if valid else None,
    }


def calculate_bvps_series(
    total_equity: List[Optional[float]],
    shares: List[Optional[float]],
) -> Dict[str, Any]:
    """Calculate book value per share series.

    Formula: BVPS_t = total_equity_t / shares_t

    Args:
        total_equity: Total equity series (oldest first, absolute)
        shares: Share count series (oldest first, same units)

    Returns:
        Dictionary with bvps_series, latest_bvps, years_used
    """
    bvps_series: List[Optional[float]] = []
    for eq, sh in zip(total_equity, shares):
        if eq is None or sh is None or sh <= 0:
            bvps_series.append(None)
        else:
            bvps_series.append(round(eq / sh, 4))
    valid = [v for v in bvps_series if v is not None]
    return {
        "bvps_series": bvps_series,
        "latest_bvps": valid[-1] if valid else None,
        "years_used": len(valid),
    }


def calculate_justified_pb(
    roe: float,
    cost_of_equity: float,
    growth: float,
    book_value_per_share: Optional[float] = None,
) -> Dict[str, Any]:
    """Calculate the justified price-to-book ratio.

    Formula: P/B* = (ROE - g) / (r - g)
             IV = BVPS x P/B*

    Insurers earning ROE above their cost of equity deserve P/B > 1;
    below it, P/B < 1.

    Args:
        roe: Sustainable return on equity as percentage (e.g., 12 for 12%)
        cost_of_equity: Cost of equity as percentage (e.g., 10 for 10%)
        growth: Sustainable growth as percentage (g < r required)
        book_value_per_share: Current book value per share (optional;
            when supplied, intrinsic_value is included

    Returns:
        Dictionary with justified_pb and optionally intrinsic_value,
        plus a full formula breakdown

    Raises:
        ValidationError: If growth >= cost of equity or inputs invalid

    Example:
        >>> calculate_justified_pb(roe=12, cost_of_equity=10, growth=4,
        ...                        book_value_per_share=50)["justified_pb"]
        1.33
    """
    if cost_of_equity <= 0:
        raise ValidationError(f"Cost of equity must be positive, got {cost_of_equity}")
    if growth >= cost_of_equity:
        raise ValidationError(
            f"Growth ({growth}%) must be below cost of equity "
            f"({cost_of_equity}%) for a finite justified P/B"
        )
    if book_value_per_share is not None and book_value_per_share <= 0:
        raise ValidationError(
            f"Book value per share must be positive, got {book_value_per_share}"
        )

    justified_pb = (roe - growth) / (cost_of_equity - growth)
    justified_pb = round(justified_pb, 4)

    if justified_pb <= 0:
        raise ValidationError(
            f"Justified P/B is non-positive ({justified_pb}): ROE ({roe}%) "
            f"must exceed growth ({growth}%) for positive value"
        )

    result: Dict[str, Any] = {
        "justified_pb": justified_pb,
        "calculation_breakdown": {
            "roe": roe,
            "cost_of_equity": cost_of_equity,
            "growth": growth,
            "formula": f"({roe} - {growth}) / ({cost_of_equity} - {growth})",
            "interpretation": (
                "ROE above cost of equity: justified P/B > 1"
                if roe > cost_of_equity
                else "ROE below cost of equity: justified P/B < 1"
            ),
        },
    }
    if book_value_per_share is not None:
        result["intrinsic_value"] = round(book_value_per_share * justified_pb, 2)
        result["book_value_per_share"] = book_value_per_share
    return result


def calculate_residual_income(
    bvps: float,
    roe_schedule: List[float],
    cost_of_equity: float,
    payout_ratio: float,
    terminal_growth: float = 0.0,
) -> Dict[str, Any]:
    """Calculate intrinsic value via the residual income model.

    Per projection year t:
        NI_t = ROE_t x BV_{t-1}
        D_t = payout x NI_t
        BV_t = BV_{t-1} + NI_t - D_t
        RI_t = (ROE_t - r) x BV_{t-1}

    Intrinsic value:
        IV = BV_0 + sum( PV(RI_t) ) + PV(terminal value)
        Terminal value = RI_n x (1+g)/(r-g) when RI_n > 0, else 0
        (residual income faded to/below the cost of equity is worth
        nothing beyond book)

    Args:
        bvps: Current book value per share (must be positive)
        roe_schedule: ROE assumption per projection year (percent)
        cost_of_equity: Cost of equity as percentage
        payout_ratio: Dividend payout ratio as percentage (0-100)
        terminal_growth: Growth of continuing residual income (percent,
            must be at least 2pp below cost of equity; default 0)

    Returns:
        Dictionary with intrinsic_value, buy-price-ready components,
        a full per-year projection table, and terminal value details

    Raises:
        ValidationError: If any input violates model constraints

    Example:
        >>> res = calculate_residual_income(
        ...     bvps=50, roe_schedule=[12, 12], cost_of_equity=10,
        ...     payout_ratio=40)
        >>> res["intrinsic_value"] > 50
        True
    """
    if bvps <= 0:
        raise ValidationError(f"Book value per share must be positive, got {bvps}")
    if not roe_schedule:
        raise ValidationError("ROE schedule cannot be empty")
    if not (0 <= payout_ratio <= 100):
        raise ValidationError(
            f"Payout ratio must be between 0 and 100 percent, got {payout_ratio}"
        )
    if cost_of_equity <= 0 or cost_of_equity > 50:
        raise ValidationError(
            f"Cost of equity must be between 0 and 50 percent, got {cost_of_equity}"
        )
    if terminal_growth >= cost_of_equity - 2.0:
        raise ValidationError(
            f"Terminal growth ({terminal_growth}%) must be at least 2pp below "
            f"cost of equity ({cost_of_equity}%)"
        )
    if terminal_growth < -10:
        raise ValidationError(
            f"Terminal growth below -10% is not meaningful, got {terminal_growth}"
        )
    for i, roe in enumerate(roe_schedule):
        if roe <= -100:
            raise ValidationError(f"ROE year {i + 1} must be > -100%, got {roe}")

    r = cost_of_equity / 100
    p = payout_ratio / 100

    projection: List[Dict[str, Any]] = []
    bv = bvps
    ri_values: List[float] = []
    pv_sum = 0.0

    for t, roe in enumerate(roe_schedule, start=1):
        bv_start = bv
        ni = roe / 100 * bv_start
        dividend = p * ni
        bv_end = bv_start + ni - dividend
        residual_income = ni - r * bv_start
        pv_factor = 1 / (1 + r) ** t
        pv_ri = residual_income * pv_factor
        ri_values.append(residual_income)
        pv_sum += pv_ri
        projection.append(
            {
                "year": t,
                "roe": roe,
                "bv_start": round(bv_start, 4),
                "net_income": round(ni, 4),
                "dividend": round(dividend, 4),
                "bv_end": round(bv_end, 4),
                "residual_income": round(residual_income, 4),
                "pv_factor": round(pv_factor, 6),
                "pv_residual_income": round(pv_ri, 4),
            }
        )
        bv = bv_end

    years = len(roe_schedule)
    final_ri = ri_values[-1]

    terminal_value = 0.0
    terminal_detail = {
        "terminal_residual_income": round(final_ri, 4),
        "terminal_roe": roe_schedule[-1],
        "terminal_growth": terminal_growth,
        "method": "none (residual income faded to/below cost of equity)",
    }
    if final_ri > 0:
        g = terminal_growth / 100
        terminal_value = final_ri * (1 + g) / (r - g)
        terminal_detail["method"] = (
            f"continuing RI: {round(final_ri, 4)} x (1+{terminal_growth}%) / "
            f"({cost_of_equity}%-{terminal_growth}%)"
        )
    pv_terminal = terminal_value / (1 + r) ** years

    intrinsic_value = bvps + pv_sum + pv_terminal

    return {
        "method": "residual_income",
        "bvps": round(bvps, 4),
        "roe_schedule": [round(x, 2) for x in roe_schedule],
        "cost_of_equity": cost_of_equity,
        "payout_ratio": payout_ratio,
        "terminal_growth": terminal_growth,
        "projection_years": years,
        "projection_table": projection,
        "dividends_per_share": [row["dividend"] for row in projection],
        "pv_residual_income_sum": round(pv_sum, 4),
        "terminal_value": round(terminal_value, 4),
        "pv_terminal_value": round(pv_terminal, 4),
        "terminal_bvps": round(bv, 4),
        "intrinsic_value": round(intrinsic_value, 2),
        "value_composition": {
            "book_value_pct": (
                round(bvps / intrinsic_value * 100, 1) if intrinsic_value > 0 else None
            ),
            "pv_residual_income_pct": (
                round(pv_sum / intrinsic_value * 100, 1)
                if intrinsic_value > 0
                else None
            ),
            "pv_terminal_pct": (
                round(pv_terminal / intrinsic_value * 100, 1)
                if intrinsic_value > 0
                else None
            ),
        },
        "terminal_detail": terminal_detail,
    }


def calculate_ddm(
    dividends: List[float],
    cost_of_equity: float,
    terminal_growth: float,
) -> Dict[str, Any]:
    """Calculate intrinsic value from a projected dividend stream (DDM).

    Multi-stage Gordon growth:
        IV = sum( D_t / (1+r)^t ) + [D_n x (1+g) / (r-g)] / (1+r)^n

    Args:
        dividends: Projected dividends per share for years 1..n
            (all must be positive)
        cost_of_equity: Cost of equity as percentage
        terminal_growth: Terminal dividend growth (percent, >= 2pp below
            cost of equity)

    Returns:
        Dictionary with intrinsic_value, pv_dividends, terminal_value,
        and a full breakdown

    Raises:
        ValidationError: If inputs violate model constraints
    """
    if not dividends:
        raise ValidationError("Dividend stream cannot be empty")
    if any(d <= 0 for d in dividends):
        raise ValidationError("All dividends must be positive")
    if cost_of_equity <= 0 or cost_of_equity > 50:
        raise ValidationError(
            f"Cost of equity must be between 0 and 50 percent, got {cost_of_equity}"
        )
    if terminal_growth >= cost_of_equity - 2.0:
        raise ValidationError(
            f"Terminal growth ({terminal_growth}%) must be at least 2pp below "
            f"cost of equity ({cost_of_equity}%)"
        )

    r = cost_of_equity / 100
    g = terminal_growth / 100

    pv_dividends: List[float] = []
    pv_sum = 0.0
    for t, div in enumerate(dividends, start=1):
        pv = div / (1 + r) ** t
        pv_dividends.append(round(pv, 4))
        pv_sum += pv

    n = len(dividends)
    final_div = dividends[-1]
    terminal_value = final_div * (1 + g) / (r - g)
    pv_terminal = terminal_value / (1 + r) ** n
    intrinsic_value = pv_sum + pv_terminal

    return {
        "method": "dividend_discount",
        "dividends": [round(d, 4) for d in dividends],
        "cost_of_equity": cost_of_equity,
        "terminal_growth": terminal_growth,
        "projection_years": n,
        "pv_dividends": pv_dividends,
        "pv_dividends_sum": round(pv_sum, 4),
        "terminal_value": round(terminal_value, 4),
        "pv_terminal_value": round(pv_terminal, 4),
        "intrinsic_value": round(intrinsic_value, 2),
    }
