"""Configuration management for FinancialCalc MCP Server"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    cache_dir: str = "./cache"
    cache_enabled: bool = True

    yfinance_timeout: int = 30
    max_retries: int = 3
    retry_delay: int = 2

    host: str = "0.0.0.0"
    port: int = 3000
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
