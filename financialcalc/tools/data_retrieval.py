"""Data retrieval tools using Yahoo Finance with SQLite caching"""

import logging
import math
from typing import Any, Dict, List, Optional

import requests
import yfinance as yf

from financialcalc.utils.cache_manager import CACHE_TTL_SECONDS, SQLiteCache
from financialcalc.utils.config import settings
from financialcalc.utils.error_handling import DataRetrievalError, ValidationError
from financialcalc.utils.session_manager import session_manager

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    """Normalize ticker symbol for Yahoo Finance compatibility.

    Yahoo Finance uses hyphens instead of dots for class shares.
    e.g., BRK.B -> BRK-B, BF.B -> BF-B
    """
    return symbol.replace(".", "-")


def _safe_float(value: Any) -> Optional[float]:
    """Convert value to float, returning None for NaN or invalid values."""
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    """Convert value to int, returning None for invalid values."""
    f = _safe_float(value)
    if f is None:
        return None
    return int(f)


def _get_cache() -> SQLiteCache:
    """Get a configured cache instance."""
    cache_db_path = f"{settings.cache_dir}/financial_cache.db"
    return SQLiteCache(cache_db_path)


def _extract_series(statement: Any, field: str, years: int) -> List[Optional[float]]:
    """Extract a time series from a financial statement.

    Returns values in chronological order (oldest first).
    yfinance returns DataFrames with newest columns first, so we
    take head(years) then reverse to get oldest-first ordering.
    """
    try:
        series = statement.loc[field].head(years)
        values = [_safe_float(v) for v in series.tolist()]
        return list(reversed(values))
    except (KeyError, AttributeError):
        return []


def _get_dates(statement: Any, years: int) -> List[str]:
    """Extract date labels from a financial statement.

    Returns dates in chronological order (oldest first) to match
    the reversed value ordering from _extract_series.
    """
    try:
        first_row = statement.iloc[:, 0:years]
        dates = first_row.columns.tolist()
        # Reverse to match _extract_series ordering (oldest first)
        dates = list(reversed(dates))
        return [
            d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d) for d in dates
        ]
    except Exception:
        return []


def _get_yf_ticker(symbol: str) -> yf.Ticker:
    """Create a yfinance Ticker with normalized symbol."""
    normalized = _normalize_symbol(symbol)
    if normalized != symbol:
        logger.info(f"Normalized symbol {symbol} -> {normalized} for yfinance")
    return yf.Ticker(normalized)


def _get_exchange_rate_to_usd(financial_currency: Optional[str]) -> Optional[float]:
    """Get exchange rate from financial currency to USD using yfinance.

    Returns 1.0 if already USD, or the conversion rate otherwise.
    Returns None if the rate cannot be fetched.

    Uses yfinance ticker format: CNYUSD=X, BRLUSD=X, etc.
    """
    if not financial_currency or financial_currency == "USD":
        return 1.0

    try:
        fx_symbol = f"{financial_currency}USD=X"
        fx_ticker = yf.Ticker(fx_symbol)
        rate = fx_ticker.info.get("regularMarketPrice")
        if rate is not None:
            logger.info(f"Exchange rate {fx_symbol}: {rate}")
            return float(rate)

        # Fallback: try without =X suffix
        fx_ticker2 = yf.Ticker(f"{financial_currency}USD")
        rate2 = fx_ticker2.info.get("regularMarketPrice")
        if rate2 is not None:
            logger.info(f"Exchange rate {financial_currency}USD: {rate2}")
            return float(rate2)

        logger.warning(f"Could not fetch exchange rate for {financial_currency} to USD")
        return None
    except Exception as e:
        logger.warning(f"Failed to fetch exchange rate for {financial_currency}: {e}")
        return None


def _convert_array_to_usd(
    values: List[Optional[float]], rate: float
) -> List[Optional[float]]:
    """Convert a list of monetary values to USD using the given exchange rate."""
    return [round(v * rate, 2) if v is not None else None for v in values]


def _get_shares_from_balance_sheet(
    balance_sheet: Any, years: int
) -> List[Optional[float]]:
    """Try to get shares outstanding from balance sheet data."""
    for field in [
        "Ordinary Shares Number",
        "Share Issued",
        "Common Stock Shares Outstanding",
        "Common Shares Outstanding",
    ]:
        values = _extract_series(balance_sheet, field, years)
        if values:
            return values
    return []


def _get_diluted_shares_from_income_stmt(
    income_stmt: Any, years: int
) -> List[Optional[float]]:
    """Derive diluted shares outstanding from net income / diluted EPS.

    yfinance income statements include "Diluted EPS" and "Net Income" rows.
    Diluted shares = net_income / diluted_eps captures all dilutive securities
    (options, RSUs, warrants, convertibles).

    Returns a time series (oldest first) of diluted share counts, or empty list
    if the required rows are unavailable.
    """
    diluted_eps = _extract_series(income_stmt, "Diluted EPS", years)
    net_income = _extract_series(income_stmt, "Net Income", years)

    if not diluted_eps or not net_income:
        return []

    result: List[Optional[float]] = []
    for eps, ni in zip(diluted_eps, net_income):
        if eps is not None and ni is not None and eps > 0:
            result.append(ni / eps)
        else:
            result.append(None)
    return result


def _get_diluted_shares_from_info(info: Dict[str, Any]) -> Optional[int]:
    """Derive diluted shares from market_cap / current_price.

    Market capitalization inherently prices in future dilution from options
    and RSUs, so market_cap / price gives a diluted-equivalent share count.
    This is the same logic yfinance uses for 'impliedSharesOutstanding'.

    Returns None if market_cap or price is missing/invalid.
    """
    market_cap = _safe_float(info.get("marketCap"))
    price = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
    if market_cap is not None and market_cap > 0 and price is not None and price > 0:
        return int(market_cap / price)
    return None


def _resolve_shares_outstanding(
    ticker: Optional[yf.Ticker],
    info: Optional[Dict[str, Any]],
    income_stmt: Any = None,
    balance_sheet: Any = None,
    years: int = 5,
) -> Dict[str, Optional[int]]:
    """Unified share-count resolver with consistent fallback logic.

    Returns a dict with:
        - 'shares_outstanding': basic/implied count (for compatibility)
        - 'diluted_shares_outstanding': diluted count (preferred for DCF)

    Resolution order for diluted:
        1. market_cap / price (most accurate, captures all dilution)
        2. net_income / diluted_eps from income statement
        3. impliedSharesOutstanding from info
        4. Fall back to basic shares (last resort)

    Resolution order for basic:
        1. impliedSharesOutstanding (captures multi-class, ~basic)
        2. sharesOutstanding (filing cover page)
        3. Balance sheet fields
    """
    if info is None and ticker is not None:
        info = ticker.info
    if info is None:
        info = {}

    # --- Diluted ---
    diluted = _get_diluted_shares_from_info(info)
    if diluted is None and income_stmt is not None:
        ds_series = _get_diluted_shares_from_income_stmt(income_stmt, years)
        if ds_series:
            for v in reversed(ds_series):
                if v is not None and v > 0:
                    diluted = int(v)
                    break
    if diluted is None:
        diluted = _safe_int(info.get("impliedSharesOutstanding"))

    # --- Basic (unchained for backward compatibility) ---
    basic = _safe_int(info.get("impliedSharesOutstanding"))
    if basic is None:
        basic = _safe_int(info.get("sharesOutstanding"))
    if basic is None and balance_sheet is not None:
        bs_shares = _get_shares_from_balance_sheet(balance_sheet, years)
        if bs_shares:
            for v in reversed(bs_shares):
                if v is not None:
                    basic = int(v)
                    break

    # If diluted failed entirely, use basic as the fallback.
    if diluted is None:
        diluted = basic

    return {
        "shares_outstanding": basic,
        "diluted_shares_outstanding": diluted,
    }


def _get_overview_from_alpha_vantage(symbol: str) -> Dict[str, Any]:
    """Fetch market cap and shares outstanding from Alpha Vantage OVERVIEW.

    A single OVERVIEW call returns both MarketCapitalization and
    SharesOutstanding. This is used as a last-resort fallback when Yahoo
    Finance returns invalid (None/NaN/zero) data for both fields.

    Results are cached for 7 days to conserve the 25 calls/day free-tier
    quota. If no API key is configured, the request fails, or the data
    is missing, an empty dict is returned (never raises).

    Args:
        symbol: Stock ticker symbol

    Returns:
        Dict with optional keys 'market_cap' (int) and
        'shares_outstanding' (int). Empty dict if unavailable.
    """
    api_key = settings.alpha_vantage_api_key
    if not api_key:
        logger.debug(
            f"Alpha Vantage fallback skipped for {symbol}: no API key configured"
        )
        return {}

    cache = _get_cache()
    cache_key = "alpha_vantage_overview"

    cached = cache.get(symbol, cache_key)
    if cached:
        logger.info(f"Alpha Vantage overview cache hit for {symbol}")
        return cached

    try:
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "OVERVIEW",
            "symbol": symbol,
            "apikey": api_key,
        }
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        if not data or "Symbol" not in data:
            logger.warning(f"Alpha Vantage returned no data for {symbol}")
            return {}

        result: Dict[str, Any] = {}

        market_cap = _safe_float(data.get("MarketCapitalization"))
        if market_cap is not None and market_cap > 0:
            result["market_cap"] = int(market_cap)

        shares = _safe_float(data.get("SharesOutstanding"))
        if shares is not None and shares > 0:
            result["shares_outstanding"] = int(shares)

        if result:
            cache.set(symbol, cache_key, result, CACHE_TTL_SECONDS[cache_key])
            logger.info(
                f"Alpha Vantage fallback for {symbol}: "
                f"market_cap={result.get('market_cap')}, "
                f"shares={result.get('shares_outstanding')}"
            )
        else:
            logger.warning(
                f"Alpha Vantage returned no valid market cap or shares for {symbol}"
            )

        return result

    except requests.RequestException as e:
        logger.warning(f"Alpha Vantage request failed for {symbol}: {e}")
        return {}
    except Exception as e:
        logger.warning(f"Alpha Vantage fallback error for {symbol}: {e}")
        return {}


def get_financial_data(
    symbol: str,
    years: int = 5,
    force_refresh: bool = False,
    clear_cache: bool = False,
    include_company_info: bool = True,
) -> Dict:
    """Retrieve historical financial data from Yahoo Finance.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of data (1-10)
        force_refresh: Force fetch from Yahoo Finance, bypassing cache
        clear_cache: Clear cache for this symbol before fetching
        include_company_info: Include company name, industry, and sector metadata

    Returns:
        Dictionary with revenue, net_income, free_cash_flow, operating_cash_flow,
        capital_expenditures, stock_based_compensation, sbc_adjusted_fcf,
        shares_outstanding, basic_eps, dates, available_years, and optionally
        company_info (long_name, short_name, industry, sector, business_summary)
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")

    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    cache = _get_cache()
    cache_key = f"historical_data_v5_usd_{years}_{include_company_info}"

    if clear_cache:
        cache.invalidate(symbol, cache_key)

    if not force_refresh:
        cached = cache.get(symbol, cache_key)
        if cached:
            logger.info(f"Cache hit for {symbol} historical data ({years} years)")
            cached["cached"] = True
            # Store in session for subsequent tool calls (even when cached)
            session_manager.update_session(
                symbol,
                {
                    "financial_data": cached,
                    "symbol": symbol,
                },
            )
            logger.info(f"Stored financial data in session for {symbol} (from cache)")
            return cached

    try:
        logger.info(f"Fetching {symbol} data from Yahoo Finance")
        ticker = _get_yf_ticker(symbol)

        income_stmt = ticker.income_stmt
        cash_flow = ticker.cash_flow

        company_info = {}
        if include_company_info:
            info = ticker.info
            company_info = {
                "long_name": info.get("longName"),
                "short_name": info.get("shortName"),
                "industry": info.get("industry"),
                "sector": info.get("sector"),
                "business_summary": info.get("longBusinessSummary"),
            }

        # Get exchange rate for currency conversion to USD
        financial_currency = ticker.info.get("financialCurrency")
        exchange_rate = _get_exchange_rate_to_usd(financial_currency)
        if exchange_rate is None:
            logger.warning(
                f"Could not fetch exchange rate for {symbol} ({financial_currency}). "
                "Returning unconverted financial data."
            )

        # Extract all available fields with graceful fallback
        revenue = _extract_series(income_stmt, "Total Revenue", years)
        if not revenue:
            revenue = _extract_series(income_stmt, "Revenue", years)
        if not revenue:
            revenue = _extract_series(income_stmt, "Operating Revenue", years)

        net_income = _extract_series(income_stmt, "Net Income", years)
        eps = _extract_series(income_stmt, "Basic EPS", years)

        fcf = _extract_series(cash_flow, "Free Cash Flow", years)
        ocf = _extract_series(cash_flow, "Operating Cash Flow", years)
        capex = _extract_series(cash_flow, "Capital Expenditure", years)
        sbc = _extract_series(cash_flow, "Stock Based Compensation", years)

        # Shares outstanding — use unified resolver (diluted + basic)
        balance_sheet = ticker.balance_sheet
        shares_resolved = _resolve_shares_outstanding(
            ticker=ticker,
            info=ticker.info,
            income_stmt=income_stmt,
            balance_sheet=balance_sheet,
            years=years,
        )
        shares_outstanding = shares_resolved["shares_outstanding"]
        diluted_shares_outstanding = shares_resolved["diluted_shares_outstanding"]
        if shares_outstanding is None:
            shares_outstanding = diluted_shares_outstanding

        # Dates - use whichever statement has more years to match data length
        inc_dates = _get_dates(income_stmt, years)
        cf_dates = _get_dates(cash_flow, years)
        dates = cf_dates if len(cf_dates) >= len(inc_dates) else inc_dates

        # Ensure all arrays have consistent length (pad with None if needed)
        max_len = max(
            len(revenue),
            len(net_income),
            len(fcf),
            len(ocf),
            len(capex),
            len(sbc),
            len(eps),
        )

        # Extend dates if shorter than data (different statements have
        # different depths). Generate approximate dates for missing years.
        if len(dates) < max_len and dates:
            # Parse the oldest date and extend backwards year by year
            from datetime import datetime, timedelta

            try:
                oldest = datetime.strptime(dates[0], "%Y-%m-%d")
                missing = max_len - len(dates)
                extra_dates = []
                for i in range(missing, 0, -1):
                    extra = oldest - timedelta(days=365 * i)
                    extra_dates.append(extra.strftime("%Y-%m-%d"))
                dates = extra_dates + dates
            except (ValueError, TypeError):
                dates = ["Unknown"] * (max_len - len(dates)) + dates

        # Pad shorter arrays with None
        def _pad(arr: List, target_len: int) -> List:
            return arr + [None] * (target_len - len(arr))

        revenue = _pad(revenue, max_len)
        net_income = _pad(net_income, max_len)
        fcf = _pad(fcf, max_len)
        ocf = _pad(ocf, max_len)
        capex = _pad(capex, max_len)
        sbc = _pad(sbc, max_len)
        eps = _pad(eps, max_len)

        # Calculate SBC-adjusted FCF
        sbc_adjusted_fcf: List[Optional[float]] = []
        for i in range(max_len):
            f = fcf[i]
            s = sbc[i]
            if f is not None and s is not None:
                sbc_adjusted_fcf.append(round(f - s, 2))
            elif f is not None:
                sbc_adjusted_fcf.append(f)
            else:
                sbc_adjusted_fcf.append(None)

        # Determine available years (count non-None FCF entries)
        available_years = sum(1 for v in fcf if v is not None)

        # Convert monetary values to USD if exchange rate is available
        if exchange_rate is not None and exchange_rate != 1.0:
            revenue = _convert_array_to_usd(revenue, exchange_rate)
            net_income = _convert_array_to_usd(net_income, exchange_rate)
            fcf = _convert_array_to_usd(fcf, exchange_rate)
            ocf = _convert_array_to_usd(ocf, exchange_rate)
            capex = _convert_array_to_usd(capex, exchange_rate)
            sbc = _convert_array_to_usd(sbc, exchange_rate)
            # Recalculate SBC-adjusted FCF with converted values
            sbc_adjusted_fcf = []
            for i in range(max_len):
                f = fcf[i]
                s = sbc[i]
                if f is not None and s is not None:
                    sbc_adjusted_fcf.append(round(f - s, 2))
                elif f is not None:
                    sbc_adjusted_fcf.append(f)
                else:
                    sbc_adjusted_fcf.append(None)
            eps = _convert_array_to_usd(eps, exchange_rate)
            logger.info(
                f"Converted {symbol} financials from {financial_currency} to USD "
                f"(rate: {exchange_rate})"
            )

        data = {
            "symbol": symbol,
            "years": years,
            "available_years": available_years,
            "actual_years_available": max_len,
            "dates": dates,
            "original_currency": financial_currency,
            "exchange_rate_used": (
                round(exchange_rate, 6) if exchange_rate is not None else None
            ),
            "revenue": [round(float(r), 2) if r is not None else None for r in revenue],
            "net_income": [
                round(float(n), 2) if n is not None else None for n in net_income
            ],
            "free_cash_flow": [
                round(float(f), 2) if f is not None else None for f in fcf
            ],
            "operating_cash_flow": [
                round(float(o), 2) if o is not None else None for o in ocf
            ],
            "capital_expenditures": [
                round(float(c), 2) if c is not None else None for c in capex
            ],
            "stock_based_compensation": [
                round(float(s), 2) if s is not None else None for s in sbc
            ],
            "sbc_adjusted_fcf": sbc_adjusted_fcf,
            "shares_outstanding": shares_outstanding,
            "diluted_shares_outstanding": diluted_shares_outstanding,
            "basic_eps": [round(float(e), 2) if e is not None else None for e in eps],
            "cached": False,
        }

        if include_company_info:
            data["company_info"] = company_info

        cache.set(symbol, cache_key, data, CACHE_TTL_SECONDS["historical_fcf"])

        # Store in session for subsequent tool calls
        session_manager.update_session(
            symbol,
            {
                "financial_data": data,
                "symbol": symbol,
            },
        )
        logger.info(f"Stored financial data in session for {symbol}")

        return data

    except Exception as e:
        logger.error(f"Failed to fetch data for {symbol}: {e}")
        raise DataRetrievalError(f"Could not retrieve data for {symbol}: {str(e)}")


def get_balance_sheet(
    symbol: str,
    years: int = 5,
    force_refresh: bool = False,
    clear_cache: bool = False,
    include_company_info: bool = False,
) -> Dict:
    """Retrieve balance sheet data from Yahoo Finance.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of data (1-10)
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear cache before fetching
        include_company_info: Include company name, industry, and sector metadata

    Returns:
        Dictionary with total_assets, total_liabilities, total_equity,
        total_debt, total_cash, net_debt, short_term_debt, long_term_debt,
        cash_and_equivalents, short_term_investments, goodwill,
        intangible_assets, dates, available_years, and optionally company_info
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")

    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    cache = _get_cache()
    cache_key = f"balance_sheet_v4_usd_{years}_{include_company_info}"

    if clear_cache:
        cache.invalidate(symbol, cache_key)

    if not force_refresh:
        cached = cache.get(symbol, cache_key)
        if cached:
            logger.info(f"Cache hit for {symbol} balance sheet ({years} years)")
            cached["cached"] = True
            # Store in session for subsequent tool calls (even when cached)
            session_manager.update_session(symbol, {"balance_sheet": cached})
            logger.info(f"Stored balance sheet in session for {symbol} (from cache)")
            return cached

    try:
        logger.info(f"Fetching {symbol} balance sheet from Yahoo Finance")
        ticker = _get_yf_ticker(symbol)

        balance_sheet = ticker.balance_sheet

        company_info = {}
        if include_company_info:
            info = ticker.info
            company_info = {
                "long_name": info.get("longName"),
                "short_name": info.get("shortName"),
                "industry": info.get("industry"),
                "sector": info.get("sector"),
                "business_summary": info.get("longBusinessSummary"),
            }

        # Get exchange rate for currency conversion to USD
        financial_currency = ticker.info.get("financialCurrency")
        exchange_rate = _get_exchange_rate_to_usd(financial_currency)
        if exchange_rate is None:
            logger.warning(
                f"Could not fetch exchange rate for {symbol} ({financial_currency}). "
                "Returning unconverted balance sheet data."
            )

        # Extract fields with multiple possible names
        total_assets = _extract_series(balance_sheet, "Total Assets", years)

        total_liabilities = _extract_series(
            balance_sheet, "Total Liabilities Net Minority Interest", years
        )
        if not total_liabilities:
            total_liabilities = _extract_series(
                balance_sheet, "Total Liabilities", years
            )

        total_equity = _extract_series(
            balance_sheet, "Total Equity Gross Minority Interest", years
        )
        if not total_equity:
            total_equity = _extract_series(balance_sheet, "Stockholders Equity", years)
        if not total_equity:
            total_equity = _extract_series(balance_sheet, "Common Stock Equity", years)

        # Total Debt - try direct field first, then calculate from components
        total_debt = _extract_series(balance_sheet, "Total Debt", years)
        if not total_debt:
            st_debt = _extract_series(
                balance_sheet, "Current Debt And Capital Lease Obligation", years
            )
            lt_debt = _extract_series(
                balance_sheet, "Long Term Debt And Capital Lease Obligation", years
            )
            if st_debt or lt_debt:
                max_len = max(len(st_debt), len(lt_debt))
                total_debt = []
                for i in range(max_len):
                    st_val = st_debt[i] if i < len(st_debt) else 0
                    lt_val = lt_debt[i] if i < len(lt_debt) else 0
                    if st_val is not None or lt_val is not None:
                        total_debt.append(round((st_val or 0) + (lt_val or 0), 2))
                    else:
                        total_debt.append(None)

        # Net Debt - use yfinance's pre-calculated value if available
        net_debt = _extract_series(balance_sheet, "Net Debt", years)

        short_term_debt = _extract_series(
            balance_sheet, "Current Debt And Capital Lease Obligation", years
        )
        if not short_term_debt:
            short_term_debt = _extract_series(balance_sheet, "Short Term Debt", years)
        if not short_term_debt:
            short_term_debt = _extract_series(balance_sheet, "Current Debt", years)

        long_term_debt = _extract_series(
            balance_sheet, "Long Term Debt And Capital Lease Obligation", years
        )
        if not long_term_debt:
            long_term_debt = _extract_series(balance_sheet, "Long Term Debt", years)

        cash_and_equivalents = _extract_series(
            balance_sheet, "Cash And Cash Equivalents", years
        )
        if not cash_and_equivalents:
            cash_and_equivalents = _extract_series(
                balance_sheet,
                "Cash Cash Equivalents And Short Term Investments",
                years,
            )

        short_term_investments = _extract_series(
            balance_sheet, "Other Short Term Investments", years
        )
        if not short_term_investments:
            short_term_investments = _extract_series(
                balance_sheet, "Short Term Investments", years
            )

        # Calculate total cash (cash + short-term investments)
        total_cash: List[Optional[float]] = []
        if cash_and_equivalents:
            for i in range(len(cash_and_equivalents)):
                c = cash_and_equivalents[i]
                s = (
                    short_term_investments[i]
                    if short_term_investments and i < len(short_term_investments)
                    else 0
                )
                if c is not None:
                    total_cash.append(round(c + (s or 0), 2))
                else:
                    total_cash.append(None)

        goodwill = _extract_series(balance_sheet, "Goodwill", years)
        intangible_assets = _extract_series(balance_sheet, "Intangible Assets", years)
        if not intangible_assets:
            intangible_assets = _extract_series(
                balance_sheet, "Net Intangible Assets", years
            )

        # Dates (chronological, oldest first)
        dates = _get_dates(balance_sheet, years)

        # Determine available years
        available_years = sum(1 for v in (total_assets or []) if v is not None)

        # Convert monetary values to USD if exchange rate is available
        if exchange_rate is not None and exchange_rate != 1.0:
            total_assets = _convert_array_to_usd(total_assets, exchange_rate)
            total_liabilities = _convert_array_to_usd(total_liabilities, exchange_rate)
            total_equity = _convert_array_to_usd(total_equity, exchange_rate)
            total_debt = _convert_array_to_usd(total_debt, exchange_rate)
            net_debt = _convert_array_to_usd(net_debt, exchange_rate)
            total_cash = _convert_array_to_usd(total_cash, exchange_rate)
            short_term_debt = _convert_array_to_usd(short_term_debt, exchange_rate)
            long_term_debt = _convert_array_to_usd(long_term_debt, exchange_rate)
            cash_and_equivalents = _convert_array_to_usd(
                cash_and_equivalents, exchange_rate
            )
            short_term_investments = _convert_array_to_usd(
                short_term_investments, exchange_rate
            )
            goodwill = _convert_array_to_usd(goodwill, exchange_rate)
            intangible_assets = _convert_array_to_usd(intangible_assets, exchange_rate)
            logger.info(
                f"Converted {symbol} balance sheet from {financial_currency} to USD "
                f"(rate: {exchange_rate})"
            )

        data = {
            "symbol": symbol,
            "years": years,
            "available_years": available_years,
            "actual_years_available": len(dates),
            "dates": dates,
            "original_currency": financial_currency,
            "exchange_rate_used": (
                round(exchange_rate, 6) if exchange_rate is not None else None
            ),
            "total_assets": [
                round(float(v), 2) if v is not None else None for v in total_assets
            ],
            "total_liabilities": [
                round(float(v), 2) if v is not None else None for v in total_liabilities
            ],
            "total_equity": [
                round(float(v), 2) if v is not None else None for v in total_equity
            ],
            "total_debt": [
                round(float(v), 2) if v is not None else None for v in total_debt
            ],
            "net_debt": [
                round(float(v), 2) if v is not None else None for v in net_debt
            ],
            "total_cash": [
                round(float(v), 2) if v is not None else None for v in total_cash
            ],
            "short_term_debt": [
                round(float(v), 2) if v is not None else None for v in short_term_debt
            ],
            "long_term_debt": [
                round(float(v), 2) if v is not None else None for v in long_term_debt
            ],
            "cash_and_equivalents": [
                round(float(v), 2) if v is not None else None
                for v in cash_and_equivalents
            ],
            "short_term_investments": [
                round(float(v), 2) if v is not None else None
                for v in short_term_investments
            ],
            "goodwill": [
                round(float(v), 2) if v is not None else None for v in goodwill
            ],
            "intangible_assets": [
                round(float(v), 2) if v is not None else None for v in intangible_assets
            ],
            "cached": False,
        }

        if include_company_info:
            data["company_info"] = company_info

        cache.set(symbol, cache_key, data, CACHE_TTL_SECONDS["historical_fcf"])

        # Store in session for subsequent tool calls
        session_manager.update_session(symbol, {"balance_sheet": data})
        logger.info(f"Stored balance sheet in session for {symbol}")

        return data

    except Exception as e:
        logger.error(f"Failed to fetch balance sheet for {symbol}: {e}")
        raise DataRetrievalError(
            f"Could not retrieve balance sheet for {symbol}: {str(e)}"
        )


def get_raw_financial_statements(
    symbol: str,
    statement_type: str = "income",
    years: int = 5,
    force_refresh: bool = False,
    clear_cache: bool = False,
    include_company_info: bool = False,
) -> Dict:
    """Retrieve raw financial statement data from Yahoo Finance.

    Args:
        symbol: Stock ticker symbol
        statement_type: One of 'income', 'cashflow', 'balance'
        years: Number of years of data (1-10)
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear cache before fetching
        include_company_info: Include company name, industry, and sector metadata

    Returns:
        Dictionary with statement_type, dates, and all line items as key-value pairs,
        and optionally company_info
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")

    if statement_type not in ("income", "cashflow", "balance"):
        raise ValidationError(
            "statement_type must be 'income', 'cashflow', or 'balance'"
        )

    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    cache = _get_cache()
    cache_key = f"raw_{statement_type}_v4_usd_{years}_{include_company_info}"

    if clear_cache:
        cache.invalidate(symbol, cache_key)

    if not force_refresh:
        cached = cache.get(symbol, cache_key)
        if cached:
            logger.info(f"Cache hit for {symbol} raw {statement_type} ({years} years)")
            cached["cached"] = True
            return cached

    try:
        logger.info(f"Fetching {symbol} raw {statement_type} from Yahoo Finance")
        ticker = _get_yf_ticker(symbol)

        company_info = {}
        if include_company_info:
            info = ticker.info
            company_info = {
                "long_name": info.get("longName"),
                "short_name": info.get("shortName"),
                "industry": info.get("industry"),
                "sector": info.get("sector"),
                "business_summary": info.get("longBusinessSummary"),
            }

        # Get exchange rate for currency conversion to USD
        financial_currency = ticker.info.get("financialCurrency")
        exchange_rate = _get_exchange_rate_to_usd(financial_currency)
        if exchange_rate is None:
            logger.warning(
                f"Could not fetch exchange rate for {symbol} ({financial_currency}). "
                "Returning unconverted raw statement data."
            )

        if statement_type == "income":
            statement = ticker.income_stmt
        elif statement_type == "cashflow":
            statement = ticker.cash_flow
        else:
            statement = ticker.balance_sheet

        # Dates (chronological, oldest first)
        dates = _get_dates(statement, years)

        # Extract all rows as key-value pairs
        line_items: Dict[str, List[Optional[float]]] = {}
        for row_label in statement.index:
            values = _extract_series(statement, row_label, years)
            if exchange_rate is not None and exchange_rate != 1.0:
                values = _convert_array_to_usd(values, exchange_rate)
            line_items[str(row_label)] = [
                round(float(v), 2) if v is not None else None for v in values
            ]

        data = {
            "symbol": symbol,
            "statement_type": statement_type,
            "years": years,
            "actual_years_available": len(dates),
            "dates": dates,
            "original_currency": financial_currency,
            "exchange_rate_used": (
                round(exchange_rate, 6) if exchange_rate is not None else None
            ),
            "line_items": line_items,
            "cached": False,
        }

        if include_company_info:
            data["company_info"] = company_info

        cache.set(symbol, cache_key, data, CACHE_TTL_SECONDS["historical_fcf"])

        return data

    except Exception as e:
        logger.error(f"Failed to fetch raw {statement_type} for {symbol}: {e}")
        raise DataRetrievalError(
            f"Could not retrieve {statement_type} statement for {symbol}: " f"{str(e)}"
        )


def get_current_metrics(
    symbol: str,
    force_refresh: bool = False,
    clear_cache: bool = False,
    include_company_info: bool = True,
) -> Dict:
    """Retrieve current market data.

    Args:
        symbol: Stock ticker symbol
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear cache for this symbol
        include_company_info: Include company name, industry, and sector metadata

    Returns:
        Dictionary with current_price, market_cap, shares_outstanding,
        previous_close, and optionally company_info (long_name, short_name,
        industry, sector, business_summary)
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")

    cache = _get_cache()

    if clear_cache:
        cache.invalidate(symbol, "current_price")
        cache.invalidate(symbol, "market_cap")
        cache.invalidate(symbol, "shares_outstanding")

    if not force_refresh:
        cached_price = cache.get(symbol, "current_price")
        cached_mc = cache.get(symbol, "market_cap")
        cached_shares = cache.get(symbol, "shares_outstanding")
        cached_company = (
            cache.get(symbol, "company_info") if include_company_info else None
        )

        if cached_price and cached_mc and cached_shares:
            result = {
                "symbol": symbol,
                "current_price": cached_price["price"],
                "market_cap": cached_mc["market_cap"],
                "shares_outstanding": cached_shares["shares"],
                "diluted_shares_outstanding": cached_shares.get(
                    "diluted", cached_shares["shares"]
                ),
                "previous_close": cached_price.get("previous_close"),
                "trading_currency": cached_price.get("trading_currency", "USD"),
                "cached": True,
            }
            if include_company_info and cached_company:
                result["company_info"] = cached_company
            # Store in session for subsequent tool calls (even when cached)
            session_manager.update_session(symbol, {"current_metrics": result})
            logger.info(f"Stored current metrics in session for {symbol} (from cache)")
            return result

    try:
        ticker = _get_yf_ticker(symbol)
        info = ticker.info

        company_info = {}
        if include_company_info:
            company_info = {
                "long_name": info.get("longName"),
                "short_name": info.get("shortName"),
                "industry": info.get("industry"),
                "sector": info.get("sector"),
                "business_summary": info.get("longBusinessSummary"),
            }

        # Try multiple price fields for robustness
        current_price = info.get("currentPrice")
        if current_price is None:
            current_price = info.get("regularMarketPrice")
        if current_price is None:
            current_price = info.get("regularMarketPreviousClose")
        if current_price is None:
            current_price = info.get("previousClose")
        if current_price is None:
            raise DataRetrievalError(f"Current price not available for {symbol}")

        previous_close = info.get("previousClose")
        if previous_close is None:
            previous_close = info.get("regularMarketPreviousClose")

        # Resolve shares using the unified resolver (diluted + basic)
        shares_resolved = _resolve_shares_outstanding(
            ticker=ticker,
            info=info,
            income_stmt=ticker.income_stmt,
        )
        shares_outstanding = shares_resolved["shares_outstanding"] or 0
        diluted_shares_outstanding = shares_resolved["diluted_shares_outstanding"] or 0

        market_cap = info.get("marketCap", 0)

        # Fallback: if market_cap is invalid (None/NaN/0/non-numeric),
        # try computing from shares x price, then Alpha Vantage as last resort
        mc_float = _safe_float(market_cap)
        if mc_float is None or mc_float <= 0:
            # Prefer diluted shares for the computation
            shares_for_calc = diluted_shares_outstanding or shares_outstanding
            shares_int = _safe_int(shares_for_calc)
            price_float = _safe_float(current_price)
            if (
                shares_int is not None
                and shares_int > 0
                and price_float is not None
                and price_float > 0
            ):
                market_cap = int(shares_int * price_float)
                logger.info(
                    f"Computed market_cap for {symbol} from "
                    f"shares_outstanding x current_price"
                )
            else:
                av_data = _get_overview_from_alpha_vantage(symbol)
                if av_data:
                    av_mc = av_data.get("market_cap")
                    av_shares = av_data.get("shares_outstanding")
                    if av_mc is not None:
                        market_cap = av_mc
                    if av_shares is not None and (
                        shares_int is None or shares_int <= 0
                    ):
                        shares_outstanding = av_shares
                        if not diluted_shares_outstanding:
                            diluted_shares_outstanding = av_shares
                    logger.info(f"Used Alpha Vantage fallback data for {symbol}")

        price_data = {
            "price": current_price,
            "previous_close": previous_close,
            "trading_currency": info.get("currency", "USD"),
        }
        cache.set(
            symbol,
            "current_price",
            price_data,
            CACHE_TTL_SECONDS["current_price"],
        )

        mc_data = {"market_cap": market_cap}
        cache.set(symbol, "market_cap", mc_data, CACHE_TTL_SECONDS["market_cap"])

        shares_data = {
            "shares": shares_outstanding,
            "diluted": diluted_shares_outstanding or shares_outstanding,
        }
        cache.set(
            symbol,
            "shares_outstanding",
            shares_data,
            CACHE_TTL_SECONDS["shares_outstanding"],
        )

        if include_company_info:
            cache.set(
                symbol,
                "company_info",
                company_info,
                CACHE_TTL_SECONDS["current_price"],
            )

        result = {
            "symbol": symbol,
            "current_price": round(current_price, 2),
            "market_cap": int(market_cap),
            "shares_outstanding": int(shares_outstanding),
            "diluted_shares_outstanding": (
                int(diluted_shares_outstanding)
                if diluted_shares_outstanding
                else int(shares_outstanding)
            ),
            "previous_close": (round(previous_close, 2) if previous_close else None),
            "trading_currency": info.get("currency", "USD"),
            "cached": False,
        }

        if include_company_info:
            result["company_info"] = company_info

        # Store in session for subsequent tool calls
        session_manager.update_session(symbol, {"current_metrics": result})
        logger.info(f"Stored current metrics in session for {symbol}")

        return result

    except DataRetrievalError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch current metrics for {symbol}: {e}")
        raise DataRetrievalError(
            f"Could not retrieve current metrics for {symbol}: {str(e)}"
        )


def get_dividend_history(
    symbol: str,
    years: int = 10,
    force_refresh: bool = False,
    clear_cache: bool = False,
) -> Dict:
    """Retrieve annual dividend-per-share history.

    Sums the per-share dividend payments reported by Yahoo Finance by
    calendar year, returning the last `years` years with data (oldest
    first). Values are converted to USD with the same exchange rate
    convention as the financial statements (financialCurrency), so DPS
    is directly comparable with the EPS series from get_financial_data.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of dividend history (1-10)
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear dividend cache for this symbol

    Returns:
        Dictionary with years_list, dividends_per_share (USD, oldest
        first), total_dividend_years, and cached flag. Empty lists mean
        the company pays no dividends (not an error).

    Raises:
        ValidationError: If symbol or years invalid
        DataRetrievalError: If Yahoo Finance fails
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")
    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    cache = _get_cache()
    cache_key = f"dividends_v1_{years}"

    if clear_cache:
        cache.invalidate(symbol, cache_key)

    if not force_refresh:
        cached = cache.get(symbol, cache_key)
        if cached and cached.get("dividends_per_share") is not None:
            cached["cached"] = True
            return cached

    try:
        ticker = _get_yf_ticker(symbol)
        dividends = ticker.dividends

        yearly: Dict[int, float] = {}
        if dividends is not None and not dividends.empty:
            for dt, amount in dividends.items():
                value = _safe_float(amount)
                if value is not None:
                    yearly[int(dt.year)] = yearly.get(int(dt.year), 0.0) + value

        sorted_years = sorted(yearly.keys())[-years:]
        dps: List[Optional[float]] = [round(yearly[y], 4) for y in sorted_years]

        financial_currency = ticker.info.get("financialCurrency")
        exchange_rate = _get_exchange_rate_to_usd(financial_currency)
        if exchange_rate is not None and exchange_rate != 1.0:
            dps = _convert_array_to_usd(dps, exchange_rate)

        result = {
            "symbol": symbol,
            "years_list": [str(y) for y in sorted_years],
            "dividends_per_share": dps,
            "total_dividend_years": len(dps),
            "cached": False,
        }
        cache.set(symbol, cache_key, result, CACHE_TTL_SECONDS["dividends"])
        return result
    except DataRetrievalError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch dividend history for {symbol}: {e}")
        raise DataRetrievalError(
            f"Could not retrieve dividend history for {symbol}: {str(e)}"
        )


def get_yearly_close_history(
    symbol: str,
    years: int = 10,
    force_refresh: bool = False,
    clear_cache: bool = False,
) -> Dict:
    """Retrieve year-end closing prices for historical multiple analysis.

    Uses monthly bars and takes the last available close of each calendar
    year, returning the last `years` years (oldest first). Prices are in
    the trading currency of the listing and are NOT FX-converted; callers
    should only build historical per-share multiples (P/B, P/E) when the
    statements share the same currency.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of price history (1-10)
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear price-history cache for this symbol

    Returns:
        Dictionary with years_list, closes (oldest first), and cached flag

    Raises:
        ValidationError: If symbol or years invalid
        DataRetrievalError: If Yahoo Finance fails or returns no data
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")
    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    cache = _get_cache()
    cache_key = f"price_history_v1_{years}"

    if clear_cache:
        cache.invalidate(symbol, cache_key)

    if not force_refresh:
        cached = cache.get(symbol, cache_key)
        if cached and cached.get("closes") is not None:
            cached["cached"] = True
            return cached

    try:
        ticker = _get_yf_ticker(symbol)
        hist = ticker.history(period=f"{years}y", interval="1mo")

        yearly: Dict[int, float] = {}
        if hist is not None and not hist.empty:
            for dt, row in hist.iterrows():
                close = _safe_float(row.get("Close"))
                if close is not None:
                    yearly[int(dt.year)] = close

        if not yearly:
            raise DataRetrievalError(f"No price history available for {symbol}")

        sorted_years = sorted(yearly.keys())[-years:]
        closes = [round(yearly[y], 2) for y in sorted_years]

        result = {
            "symbol": symbol,
            "years_list": [str(y) for y in sorted_years],
            "closes": closes,
            "cached": False,
        }
        cache.set(symbol, cache_key, result, CACHE_TTL_SECONDS["price_history"])
        return result
    except DataRetrievalError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch price history for {symbol}: {e}")
        raise DataRetrievalError(
            f"Could not retrieve price history for {symbol}: {str(e)}"
        )
