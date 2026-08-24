"""WACC (Weighted Average Cost of Capital) calculation.

Provides a company-specific discount-rate baseline instead of a flat
assumption. For financial companies (banks/insurers), where debt is operating
material rather than financing, cost of equity alone is returned.
"""

import logging
from typing import Any, Dict, List, Optional

import yfinance as yf

from financialcalc.tools.data_retrieval import _get_cache, _get_yf_ticker, _safe_float
from financialcalc.utils.cache_manager import CACHE_TTL_SECONDS
from financialcalc.utils.config import settings

logger = logging.getLogger(__name__)

# Damodaran-style country risk premiums (percent) keyed by Yahoo country name.
# Default for unmapped countries: 1.5%.
COUNTRY_RISK_PREMIUMS: Dict[str, float] = {
    "United States": 0.0,
    "Canada": 0.3,
    "United Kingdom": 0.5,
    "Japan": 0.5,
    "Germany": 0.3,
    "France": 0.4,
    "Netherlands": 0.3,
    "Switzerland": 0.2,
    "Australia": 0.5,
    "Sweden": 0.3,
    "Denmark": 0.3,
    "Norway": 0.3,
    "Hong Kong": 0.8,
    "Singapore": 0.5,
    "Taiwan": 0.6,
    "South Korea": 0.7,
    "China": 1.0,
    "India": 2.0,
    "Brazil": 2.5,
    "Mexico": 2.0,
    "Chile": 1.2,
    "Argentina": 6.0,
    "Uruguay": 3.0,
    "Kazakhstan": 4.5,
    "Turkey": 5.5,
    "Russia": 6.5,
    "South Africa": 2.5,
    "Israel": 0.8,
    "Ireland": 0.3,
    "Bermuda": 0.3,
}

DEFAULT_COUNTRY_RISK = 1.5
FINANCIAL_SECTORS = {"Financial Services"}


def get_risk_free_rate() -> Optional[float]:
    """Fetch the 10-year US Treasury yield from Yahoo (^TNX), cached 24h.

    Returns the yield in percent, or None if unavailable (caller falls back
    to settings.risk_free_rate).
    """
    cache = _get_cache()
    cached = cache.get("TREASURY_10Y", "risk_free_rate")
    if cached and cached.get("rate") is not None:
        return float(cached["rate"])

    try:
        rate = yf.Ticker("^TNX").fast_info.get("last_price")
        if rate is not None and 0 < rate < 20:
            # ^TNX quotes the yield directly (e.g. 4.32 = 4.32%). Guard
            # against a fractional quote (0.0432) by scaling.
            if rate < 1.0:
                rate = rate * 100.0
            rate = round(float(rate), 2)
            cache.set(
                "TREASURY_10Y",
                "risk_free_rate",
                {"rate": rate},
                CACHE_TTL_SECONDS["risk_free_rate"],
            )
            return rate
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not fetch risk-free rate: {e}")
    return None


def get_country_risk_premium(country: Optional[str]) -> float:
    """Return the country risk premium in percent for a Yahoo country name."""
    if not country:
        return DEFAULT_COUNTRY_RISK
    return COUNTRY_RISK_PREMIUMS.get(country.strip(), DEFAULT_COUNTRY_RISK)


def _latest(series: Optional[List[Optional[float]]]) -> Optional[float]:
    """Return the most recent non-None value of a series (oldest first)."""
    if not series:
        return None
    for v in reversed(series):
        if v is not None:
            return v
    return None


def calculate_wacc(symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Calculate WACC for a company from Yahoo Finance data.

    Method:
        - Cost of equity: CAPM + country risk premium
          Re = rf + beta x ERP + CRP
        - Cost of debt (pre-tax): Interest Expense / Total Debt,
          falling back to rf + 2.0% credit spread
        - Tax rate: Tax Provision / Pretax Income, fallback 21%
        - Weights: marketCap for equity, balance-sheet Total Debt for debt
        - Financial companies: cost of equity only (debt is operating
          material; WACC is conceptually wrong for banks/insurers)
        - Final value clamped to [wacc_min, wacc_max] (default 6-15%)

    Args:
        symbol: Stock ticker symbol
        force_refresh: Bypass the 7-day wacc_inputs cache

    Returns:
        Dictionary with wacc (percent or None), cost_of_equity,
        cost_of_debt, tax_rate, weights, inputs_used, financials_fallback,
        clamped, and warnings. wacc is None only when equity inputs are
        unavailable; callers should then use 10.0 as fallback.
    """
    cache = _get_cache()
    if not force_refresh:
        cached = cache.get(symbol, "wacc_inputs")
        if cached and cached.get("wacc") is not None:
            cached["cached"] = True
            return cached

    warnings: List[str] = []
    ticker = _get_yf_ticker(symbol)
    info = ticker.info

    rf = get_risk_free_rate()
    rf_source = "^TNX"
    if rf is None:
        rf = settings.risk_free_rate
        rf_source = f"fallback constant ({settings.risk_free_rate}%)"
        warnings.append(
            f"Risk-free rate fetch failed; using {settings.risk_free_rate}% fallback."
        )

    beta = _safe_float(info.get("beta"))
    beta_source = "info.beta"
    if beta is None or beta <= 0:
        beta = 1.0
        beta_source = "fallback 1.0"
        warnings.append("Beta unavailable; using 1.0.")

    country = info.get("country")
    crp = get_country_risk_premium(country)

    erp = settings.equity_risk_premium
    cost_of_equity = round(rf + beta * erp + crp, 2)

    sector = info.get("sector") or ""
    is_financial = sector in FINANCIAL_SECTORS

    market_cap = _safe_float(info.get("marketCap"))

    result: Dict[str, Any] = {
        "symbol": symbol,
        "cost_of_equity": cost_of_equity,
        "financials_fallback": is_financial,
        "inputs_used": {
            "risk_free_rate": rf,
            "risk_free_source": rf_source,
            "beta": beta,
            "beta_source": beta_source,
            "equity_risk_premium": erp,
            "country_risk_premium": crp,
            "country": country,
            "sector": sector,
        },
        "warnings": warnings,
    }

    if is_financial:
        raw = cost_of_equity
        clamped = False
        if raw < settings.wacc_min:
            raw, clamped = settings.wacc_min, True
        elif raw > settings.wacc_max:
            raw, clamped = settings.wacc_max, True
        result.update(
            {
                "wacc": raw,
                "clamped": clamped,
                "cost_of_debt": None,
                "tax_rate": None,
                "weights": None,
                "method": "cost_of_equity_only (financial company)",
            }
        )
        cache.set(symbol, "wacc_inputs", result, CACHE_TTL_SECONDS["wacc_inputs"])
        return result

    # Non-financial: full WACC with debt weights.
    try:
        balance_sheet = ticker.balance_sheet
        debt_series = []
        for field in ("Total Debt", "Current Debt And Capital Lease Obligation"):
            try:
                row = balance_sheet.loc[field].head(1)
                debt_series = [_safe_float(v) for v in row.tolist()]
                if debt_series and debt_series[0] is not None:
                    break
            except KeyError:
                continue
        total_debt = debt_series[0] if debt_series else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Balance sheet fetch failed for {symbol}: {e}")
        total_debt = None

    income_stmt = ticker.income_stmt

    def _stmt_field(field: str) -> Optional[float]:
        try:
            row = income_stmt.loc[field].head(1)
            vals = [_safe_float(v) for v in row.tolist()]
            return vals[0] if vals else None
        except KeyError:
            return None

    interest_expense = _stmt_field("Interest Expense")
    tax_provision = _stmt_field("Tax Provision")
    pretax_income = _stmt_field("Pretax Income")

    if interest_expense and total_debt and total_debt > 0:
        cost_of_debt = round(abs(interest_expense) / total_debt * 100, 2)
        debt_source = "interest_expense / total_debt"
    else:
        cost_of_debt = round(rf + 2.0, 2)
        debt_source = "rf + 2.0% spread (fallback)"
        warnings.append(
            "Interest expense or total debt unavailable; cost of debt estimated."
        )

    if tax_provision and pretax_income and pretax_income > 0:
        tax_rate = round(min(tax_provision / pretax_income, 0.40) * 100, 1)
        tax_source = "tax_provision / pretax_income"
    else:
        tax_rate = settings.default_corporate_tax_rate
        tax_source = "fallback constant"
        warnings.append("Tax inputs unavailable; using default tax rate.")

    result["inputs_used"].update(
        {
            "market_cap": market_cap,
            "total_debt": total_debt,
            "interest_expense": interest_expense,
            "cost_of_debt_source": debt_source,
            "tax_rate_source": tax_source,
        }
    )

    if not market_cap or market_cap <= 0 or total_debt is None or total_debt < 0:
        # Cannot weight; fall back to cost of equity.
        raw = cost_of_equity
        clamped = not (settings.wacc_min <= raw <= settings.wacc_max)
        result.update(
            {
                "wacc": max(settings.wacc_min, min(settings.wacc_max, raw)),
                "clamped": clamped,
                "cost_of_debt": cost_of_debt,
                "tax_rate": tax_rate,
                "weights": None,
                "method": "cost_of_equity fallback (weights unavailable)",
            }
        )
        warnings.append("Market cap or debt unavailable; returning cost of equity.")
        result["warnings"] = warnings
        cache.set(symbol, "wacc_inputs", result, CACHE_TTL_SECONDS["wacc_inputs"])
        return result

    equity_value = market_cap
    debt_value = total_debt
    total_value = equity_value + debt_value
    weight_equity = equity_value / total_value
    weight_debt = debt_value / total_value

    wacc_raw = weight_equity * cost_of_equity + weight_debt * cost_of_debt * (
        1 - tax_rate / 100
    )
    wacc_raw = round(wacc_raw, 2)

    clamped = False
    wacc = wacc_raw
    if wacc < settings.wacc_min:
        wacc, clamped = settings.wacc_min, True
    elif wacc > settings.wacc_max:
        wacc, clamped = settings.wacc_max, True

    result.update(
        {
            "wacc": wacc,
            "unclamped_wacc": wacc_raw,
            "clamped": clamped,
            "cost_of_debt": cost_of_debt,
            "tax_rate": tax_rate,
            "weights": {
                "equity": round(weight_equity, 4),
                "debt": round(weight_debt, 4),
            },
            "method": "full WACC",
            "warnings": warnings,
        }
    )

    cache.set(symbol, "wacc_inputs", result, CACHE_TTL_SECONDS["wacc_inputs"])
    return result
