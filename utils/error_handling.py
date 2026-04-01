"""Custom exceptions for FinancialCalc"""


class FinancialCalcError(Exception):
    """Base exception for all FinancialCalc errors"""

    pass


class ValidationError(FinancialCalcError):
    """Raised when input validation fails"""

    pass


class CalculationError(FinancialCalcError):
    """Raised when calculation fails"""

    pass


class DataRetrievalError(FinancialCalcError):
    """Raised when data retrieval fails"""

    pass


class CacheError(FinancialCalcError):
    """Raised when cache operation fails"""

    pass
