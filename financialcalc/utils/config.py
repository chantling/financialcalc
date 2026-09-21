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

    iv_tracker_db_path: str = (
        r"C:\Users\John\Documents\#Python#\#Financial#\IV-Tracker\data\iv_tracker.db"
    )

    # WACC / CAPM inputs
    risk_free_rate: float = 4.3  # fallback when ^TNX fetch fails (percent)
    equity_risk_premium: float = 5.0  # percent
    wacc_min: float = 6.0  # clamp bounds (percent)
    wacc_max: float = 15.0
    default_corporate_tax_rate: float = 21.0

    host: str = "0.0.0.0"
    port: int = 3000
    log_level: str = "INFO"

    eight_pillar_pe_max: float = 22.5
    eight_pillar_roic_min: float = 9.0
    eight_pillar_debt_to_fcf_max: float = 5.0
    eight_pillar_fcf_multiple: float = 22.5

    # Financial-institution valuation (residual income / justified P/B)
    financials_roe_min: float = 10.0  # pillar: avg ROE threshold (percent)
    financials_payout_max: float = 80.0  # pillar: sustainable payout ceiling (percent)
    financials_justified_pb_min: float = 0.5  # first-run implied P/B* band
    financials_justified_pb_max: float = 2.5
    financials_coe_min: float = 8.0  # first-run CoE band (no WACC baseline)
    financials_coe_max: float = 12.0

    # Asset-light override for provider-classified financial companies
    # (e.g., payment networks in Yahoo's "Credit Services" industry).
    # When current P/B AND average historical ROE BOTH exceed these
    # thresholds, book equity is a capital-return residual rather than an
    # operational asset, so a book-value-anchored residual-income model
    # cannot capture earnings power; the company is valued on the
    # operating-company (FCF DCF) track instead.
    asset_light_pb_min: float = 5.0  # current price / book >= threshold
    asset_light_roe_min: float = 30.0  # avg historical ROE >= threshold (percent)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
