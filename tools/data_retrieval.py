"""Data retrieval tools using Yahoo Finance with SQLite caching"""

import logging
import math
from typing import Any, Dict, List, Optional

import yfinance as yf

from financialcalc.utils.cache_manager import CACHE_TTL_SECONDS, SQLiteCache
from financialcalc.utils.config import settings
from financialcalc.utils.error_handling import DataRetrievalError, ValidationError

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


def _get_shares_from_info(ticker: yf.Ticker) -> Optional[int]:
    """Try multiple methods to get shares outstanding from ticker info."""
    info = ticker.info
    shares = _safe_int(info.get("sharesOutstanding"))
    if shares is None:
        shares = _safe_int(info.get("impliedSharesOutstanding"))
    return shares


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


def get_financial_data(
    symbol: str,
    years: int = 5,
    force_refresh: bool = False,
    clear_cache: bool = False,
) -> Dict:
    """Retrieve historical financial data from Yahoo Finance.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of data (1-10)
        force_refresh: Force fetch from Yahoo Finance, bypassing cache
        clear_cache: Clear cache for this symbol before fetching

    Returns:
        Dictionary with revenue, net_income, free_cash_flow, operating_cash_flow,
        capital_expenditures, stock_based_compensation, sbc_adjusted_fcf,
        shares_outstanding, basic_eps, dates, available_years
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")

    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    cache = _get_cache()
    cache_key = f"historical_data_v3_{years}"

    if clear_cache:
        cache.invalidate(symbol, cache_key)

    if not force_refresh:
        cached = cache.get(symbol, cache_key)
        if cached:
            logger.info(f"Cache hit for {symbol} historical data ({years} years)")
            cached["cached"] = True
            return cached

    try:
        logger.info(f"Fetching {symbol} data from Yahoo Finance")
        ticker = _get_yf_ticker(symbol)

        income_stmt = ticker.income_stmt
        cash_flow = ticker.cash_flow

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

        # Shares outstanding - try info first, then balance sheet
        shares_outstanding = _get_shares_from_info(ticker)
        if shares_outstanding is None:
            balance_sheet = ticker.balance_sheet
            bs_shares = _get_shares_from_balance_sheet(balance_sheet, years)
            if bs_shares:
                # Use the most recent non-None value
                for v in reversed(bs_shares):
                    if v is not None:
                        shares_outstanding = int(v)
                        break

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

        data = {
            "symbol": symbol,
            "years": years,
            "available_years": available_years,
            "actual_years_available": max_len,
            "dates": dates,
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
            "basic_eps": [round(float(e), 2) if e is not None else None for e in eps],
            "cached": False,
        }

        cache.set(symbol, cache_key, data, CACHE_TTL_SECONDS["historical_fcf"])

        return data

    except Exception as e:
        logger.error(f"Failed to fetch data for {symbol}: {e}")
        raise DataRetrievalError(f"Could not retrieve data for {symbol}: {str(e)}")


def get_balance_sheet(
    symbol: str,
    years: int = 5,
    force_refresh: bool = False,
    clear_cache: bool = False,
) -> Dict:
    """Retrieve balance sheet data from Yahoo Finance.

    Args:
        symbol: Stock ticker symbol
        years: Number of years of data (1-10)
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear cache before fetching

    Returns:
        Dictionary with total_assets, total_liabilities, total_equity,
        total_debt, total_cash, net_debt, short_term_debt, long_term_debt,
        cash_and_equivalents, short_term_investments, goodwill,
        intangible_assets, dates, available_years
    """
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")

    if years < 1 or years > 10:
        raise ValidationError("Years must be between 1 and 10")

    cache = _get_cache()
    cache_key = f"balance_sheet_v2_{years}"

    if clear_cache:
        cache.invalidate(symbol, cache_key)

    if not force_refresh:
        cached = cache.get(symbol, cache_key)
        if cached:
            logger.info(f"Cache hit for {symbol} balance sheet ({years} years)")
            cached["cached"] = True
            return cached

    try:
        logger.info(f"Fetching {symbol} balance sheet from Yahoo Finance")
        ticker = _get_yf_ticker(symbol)

        balance_sheet = ticker.balance_sheet

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

        data = {
            "symbol": symbol,
            "years": years,
            "available_years": available_years,
            "actual_years_available": len(dates),
            "dates": dates,
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

        cache.set(symbol, cache_key, data, CACHE_TTL_SECONDS["historical_fcf"])

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
) -> Dict:
    """Retrieve raw financial statement data from Yahoo Finance.

    Args:
        symbol: Stock ticker symbol
        statement_type: One of 'income', 'cashflow', 'balance'
        years: Number of years of data (1-10)
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear cache before fetching

    Returns:
        Dictionary with statement_type, dates, and all line items as key-value pairs
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
    cache_key = f"raw_{statement_type}_v2_{years}"

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
            line_items[str(row_label)] = [
                round(float(v), 2) if v is not None else None for v in values
            ]

        data = {
            "symbol": symbol,
            "statement_type": statement_type,
            "years": years,
            "actual_years_available": len(dates),
            "dates": dates,
            "line_items": line_items,
            "cached": False,
        }

        cache.set(symbol, cache_key, data, CACHE_TTL_SECONDS["historical_fcf"])

        return data

    except Exception as e:
        logger.error(f"Failed to fetch raw {statement_type} for {symbol}: {e}")
        raise DataRetrievalError(
            f"Could not retrieve {statement_type} statement for {symbol}: " f"{str(e)}"
        )


def get_current_metrics(
    symbol: str, force_refresh: bool = False, clear_cache: bool = False
) -> Dict:
    """Retrieve current market data.

    Args:
        symbol: Stock ticker symbol
        force_refresh: Force fetch from Yahoo Finance
        clear_cache: Clear cache for this symbol

    Returns:
        Dictionary with current_price, market_cap, shares_outstanding,
        previous_close
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

        if cached_price and cached_mc and cached_shares:
            return {
                "symbol": symbol,
                "current_price": cached_price["price"],
                "market_cap": cached_mc["market_cap"],
                "shares_outstanding": cached_shares["shares"],
                "previous_close": cached_price.get("previous_close"),
                "cached": True,
            }

    try:
        ticker = _get_yf_ticker(symbol)
        info = ticker.info

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

        market_cap = info.get("marketCap", 0)
        shares_outstanding = info.get("sharesOutstanding", 0)
        if shares_outstanding == 0:
            shares_outstanding = info.get("impliedSharesOutstanding", 0)

        price_data = {
            "price": current_price,
            "previous_close": previous_close,
        }
        cache.set(
            symbol,
            "current_price",
            price_data,
            CACHE_TTL_SECONDS["current_price"],
        )

        mc_data = {"market_cap": market_cap}
        cache.set(symbol, "market_cap", mc_data, CACHE_TTL_SECONDS["market_cap"])

        shares_data = {"shares": shares_outstanding}
        cache.set(
            symbol,
            "shares_outstanding",
            shares_data,
            CACHE_TTL_SECONDS["shares_outstanding"],
        )

        return {
            "symbol": symbol,
            "current_price": round(current_price, 2),
            "market_cap": int(market_cap),
            "shares_outstanding": int(shares_outstanding),
            "previous_close": (round(previous_close, 2) if previous_close else None),
            "cached": False,
        }

    except DataRetrievalError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch current metrics for {symbol}: {e}")
        raise DataRetrievalError(
            f"Could not retrieve current metrics for {symbol}: {str(e)}"
        )
