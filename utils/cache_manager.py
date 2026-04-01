"""SQLite cache manager with enhanced retry logic for concurrent access"""

import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from .cache_interface import CacheBackend

CACHE_TTL_SECONDS = {
    "current_price": 900,
    "market_cap": 3600,
    "historical_revenue": 604800,
    "historical_net_income": 604800,
    "historical_fcf": 604800,
    "historical_eps": 604800,
    "shares_outstanding": 604800,
    "basic_eps": 604800,
    "balance_sheet": 604800,
    "raw_statements": 604800,
}


class SQLiteCache(CacheBackend):
    """SQLite cache with enhanced retry logic for concurrent access"""

    def __init__(self, db_path: str, enabled: bool = True):
        self.enabled = enabled
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(__name__)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema and enable WAL mode"""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cached_data (
                symbol TEXT NOT NULL,
                data_type TEXT NOT NULL,
                data JSON NOT NULL,
                cached_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                last_accessed TIMESTAMP,
                access_count INTEGER DEFAULT 0,
                UNIQUE(symbol, data_type)
            )
        """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON cached_data(symbol)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expires_at ON cached_data(expires_at)"
        )
        conn.commit()
        conn.close()

    def get(self, symbol: str, data_type: str) -> Optional[Dict]:
        """Retrieve cached data if not expired"""
        if not self.enabled:
            return None

        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT data, expires_at FROM cached_data
                WHERE symbol = ? AND data_type = ?
            """,
                (symbol, data_type),
            )

            row = cursor.fetchone()
            if not row:
                conn.close()
                return None

            data_json, expires_at = row
            if datetime.fromisoformat(expires_at) < datetime.now():
                cursor.execute(
                    "DELETE FROM cached_data WHERE symbol = ? AND data_type = ?",
                    (symbol, data_type),
                )
                conn.commit()
                conn.close()
                return None

            cursor.execute(
                """
                UPDATE cached_data
                SET last_accessed = ?, access_count = access_count + 1
                WHERE symbol = ? AND data_type = ?
            """,
                (datetime.now().isoformat(), symbol, data_type),
            )
            conn.commit()
            conn.close()

            return json.loads(data_json)

        except Exception as e:
            self.logger.error(f"Cache get error: {e}")
            return None

    def set(self, symbol: str, data_type: str, data: Dict, ttl: int) -> None:
        """Set cache entry with enhanced retry logic"""
        if not self.enabled:
            return

        expires_at = (datetime.now() + timedelta(seconds=ttl)).isoformat()
        data_json = json.dumps(data)

        self._set_with_retry(symbol, data_type, data_json, expires_at)

    def _set_with_retry(
        self, symbol: str, data_type: str, data_json: str, expires_at: str
    ) -> None:
        """Write to cache with exponential backoff + jitter + delay"""
        max_retries = 5
        base_delay = 0.05
        max_delay = 1.0

        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect(str(self.db_path))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO cached_data
                    (symbol, data_type, data, expires_at, cached_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                    (symbol, data_type, data_json, expires_at),
                )
                conn.commit()
                conn.close()
                return

            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    if attempt == max_retries - 1:
                        self.logger.warning(
                            f"Cache write failed after {max_retries} retries for "
                            f"{symbol}:{data_type}"
                        )
                        return

                    delay = min(
                        base_delay * (2**attempt) + random.random() * 0.1, max_delay
                    )
                    time.sleep(delay)
                    continue
                raise

    def invalidate(self, symbol: str, data_type: Optional[str] = None) -> None:
        """Clear cache entries"""
        if not self.enabled:
            return

        try:
            conn = sqlite3.connect(str(self.db_path))
            if data_type is None:
                conn.execute("DELETE FROM cached_data WHERE symbol = ?", (symbol,))
            else:
                conn.execute(
                    "DELETE FROM cached_data WHERE symbol = ? AND data_type = ?",
                    (symbol, data_type),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.error(f"Cache invalidate error: {e}")
