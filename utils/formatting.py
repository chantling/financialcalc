"""Formatting utilities for financial data"""

from decimal import ROUND_HALF_UP, Decimal


def round_financial(value: float, decimals: int = 2) -> float:
    """Round financial values using half-away-from-zero rule"""
    return float(
        Decimal(str(value)).quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_HALF_UP)
    )


def format_percentage(value: float, decimals: int = 2) -> str:
    """Format as percentage with sign"""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def format_currency(value: float, decimals: int = 2) -> str:
    """Format as currency with commas"""
    return f"${value:,.{decimals}f}"
