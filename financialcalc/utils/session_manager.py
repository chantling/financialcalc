"""Session management for analysis workflows.

This module provides persistent session storage using SQLite to maintain state
between tool calls, eliminating the need for agents to pass data between tools.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from financialcalc.utils.cache_manager import SQLiteCache
from financialcalc.utils.config import settings

logger = logging.getLogger(__name__)

# Session TTL: 30 minutes of inactivity
SESSION_TTL_SECONDS = 30 * 60


class SessionManager:
    """Manages all active analysis sessions using persistent SQLite storage.
    
    Provides a central registry of sessions with automatic cleanup
    of expired sessions. Uses SQLite to persist across process boundaries.
    """
    
    def __init__(self):
        """Initialize the session manager with SQLite backend."""
        cache_db_path = f"{settings.cache_dir}/session_cache.db"
        self.cache = SQLiteCache(cache_db_path)
        logger.info("Session manager initialized with SQLite backend")
    
    def _get_session_key(self, session_id: str) -> str:
        """Get cache key for session data."""
        return f"session_{session_id}"
    
    def get_or_create_session(self, session_id: str) -> Dict[str, Any]:
        """Get an existing session or create a new one.
        
        Args:
            session_id: Unique identifier for the session (typically ticker symbol)
            
        Returns:
            Session data dictionary
        """
        session_key = self._get_session_key(session_id)
        
        # Try to get existing session
        session = self.cache.get(session_key, "session_data")
        
        if session:
            # Touch the session to update last accessed time
            session["last_accessed"] = datetime.now().isoformat()
            self.cache.set(session_key, "session_data", session, SESSION_TTL_SECONDS)
            logger.debug(f"Accessed existing session: {session_id}")
            return session
        
        # Create new session
        new_session = {
            "session_id": session_id,
            "symbol": session_id,
            "financial_data": None,
            "balance_sheet": None,
            "current_metrics": None,
            "calculated_cagr": None,
            "base_fcf_options": None,
            "dcf_results": None,
            "scenario_results": None,
            "created_at": datetime.now().isoformat(),
            "last_accessed": datetime.now().isoformat(),
        }
        
        self.cache.set(session_key, "session_data", new_session, SESSION_TTL_SECONDS)
        logger.info(f"Created new session: {session_id}")
        return new_session
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get an existing session without creating a new one.
        
        Args:
            session_id: Unique identifier for the session
            
        Returns:
            The session if it exists, None otherwise
        """
        session_key = self._get_session_key(session_id)
        session = self.cache.get(session_key, "session_data")
        
        if session:
            # Touch the session to update last accessed time
            session["last_accessed"] = datetime.now().isoformat()
            self.cache.set(session_key, "session_data", session, SESSION_TTL_SECONDS)
            return session
        
        return None
    
    def update_session(self, session_id: str, data: Dict[str, Any]) -> None:
        """Update session data.
        
        Args:
            session_id: Unique identifier for the session
            data: Dictionary of data to update in the session
        """
        session_key = self._get_session_key(session_id)
        
        # Get existing session or create new one
        session = self.get_session(session_id)
        if session is None:
            session = self.get_or_create_session(session_id)
        
        # Update with new data
        session.update(data)
        session["last_accessed"] = datetime.now().isoformat()
        
        # Save back to cache
        self.cache.set(session_key, "session_data", session, SESSION_TTL_SECONDS)
        logger.debug(f"Updated session: {session_id}")
    
    def session_exists(self, session_id: str) -> bool:
        """Check if a non-expired session exists.
        
        Args:
            session_id: Unique identifier for the session
            
        Returns:
            True if the session exists and is not expired
        """
        return self.get_session(session_id) is not None
    
    def delete_session(self, session_id: str) -> bool:
        """Delete a session.
        
        Args:
            session_id: Unique identifier for the session
            
        Returns:
            True if the session was deleted, False if it didn't exist
        """
        session_key = self._get_session_key(session_id)
        try:
            self.cache.invalidate(session_key, "session_data")
            logger.info(f"Deleted session: {session_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting session {session_id}: {e}")
            return False
    
    def has_financial_data(self, session_id: str) -> bool:
        """Check if financial data has been loaded into the session."""
        session = self.get_session(session_id)
        return session is not None and session.get("financial_data") is not None
    
    def has_balance_sheet(self, session_id: str) -> bool:
        """Check if balance sheet data has been loaded into the session."""
        session = self.get_session(session_id)
        return session is not None and session.get("balance_sheet") is not None
    
    def has_current_metrics(self, session_id: str) -> bool:
        """Check if current market metrics have been loaded into the session."""
        session = self.get_session(session_id)
        return session is not None and session.get("current_metrics") is not None
    
    def has_dcf_results(self, session_id: str) -> bool:
        """Check if DCF analysis has been performed."""
        session = self.get_session(session_id)
        return session is not None and session.get("dcf_results") is not None


# Global session manager instance
session_manager = SessionManager()
