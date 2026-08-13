"""Cache backend interface for FinancialCalc"""

from abc import ABC, abstractmethod
from typing import Dict, Optional


class CacheBackend(ABC):
    """Abstract base class for cache backends"""

    @abstractmethod
    def get(self, symbol: str, data_type: str) -> Optional[Dict]:
        """Retrieve cached data if not expired"""
        pass

    @abstractmethod
    def set(self, symbol: str, data_type: str, data: Dict, ttl: int) -> None:
        """Store data in cache with TTL"""
        pass

    @abstractmethod
    def invalidate(self, symbol: str, data_type: Optional[str] = None) -> None:
        """Clear cache entries for symbol"""
        pass
