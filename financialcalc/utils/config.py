"""Configuration management for FinancialCalc MCP Server"""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    cache_dir: str = "./cache"
    cache_enabled: bool = True

    yfinance_timeout: int = 30
    max_retries: int = 3
    retry_delay: int = 2

    alpha_vantage_api_key: Optional[str] = None

    host: str = "0.0.0.0"
    port: int = 3000
    log_level: str = "INFO"

    eight_pillar_pe_max: float = 22.5
    eight_pillar_roic_min: float = 9.0
    eight_pillar_debt_to_fcf_max: float = 5.0
    eight_pillar_fcf_multiple: float = 22.5

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
