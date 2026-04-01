"""Pydantic validation models for all tools"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class CAGRInput(BaseModel):
    values: List[float]
    years: Optional[int] = Field(default=None, gt=0)

    @field_validator("values")
    @classmethod
    def values_must_be_positive(cls, v):
        if len(v) < 2:
            raise ValueError("Values array must have at least 2 elements")
        if any(x <= 0 for x in v):
            raise ValueError("All values must be positive")
        return v


class CashFlowProjectionInput(BaseModel):
    base_fcf: float = Field(..., gt=0)
    growth_rates: List[float]
    years: int = Field(default=10, gt=0, le=20)

    @field_validator("growth_rates")
    @classmethod
    def growth_rates_must_match_years(cls, v, info):
        if "years" in info.data and len(v) != info.data["years"]:
            raise ValueError("Growth rates array length must match years")
        return v

    @field_validator("growth_rates")
    @classmethod
    def growth_rates_must_be_reasonable(cls, v):
        if any(r < -50 or r > 200 for r in v):
            raise ValueError("Growth rates must be between -50% and 200%")
        return v


class PresentValueInput(BaseModel):
    cash_flows: List[float]
    discount_rate: float = Field(..., gt=0, le=50)

    @field_validator("cash_flows")
    @classmethod
    def cash_flows_must_not_be_empty(cls, v):
        if not v:
            raise ValueError("Cash flows array cannot be empty")
        return v


class TerminalValueInput(BaseModel):
    final_year_fcf: float = Field(..., gt=0)
    terminal_multiple: float = Field(..., gt=0, le=50)


class IntrinsicValueInput(BaseModel):
    pv_cash_flows: float
    pv_terminal_value: float
    net_cash: float
    shares_outstanding: float = Field(..., gt=0)


class MarginOfSafetyInput(BaseModel):
    intrinsic_value: float = Field(..., gt=0)
    margin_percent: float = Field(..., ge=0, le=100)


class SensitivityAnalysisInput(BaseModel):
    base_value: float = Field(..., gt=0)
    variables: List[str]
    ranges: List[List[float]]

    @field_validator("variables")
    @classmethod
    def variables_limit(cls, v):
        if len(v) > 3:
            raise ValueError("Maximum 3 variables for sensitivity analysis")
        return v

    @field_validator("ranges")
    @classmethod
    def ranges_must_match_variables(cls, v, info):
        if "variables" in info.data and len(v) != len(info.data["variables"]):
            raise ValueError("Ranges array length must match variables")
        return v


class ProbabilityWeightedInput(BaseModel):
    values: List[float]
    probabilities: List[float]

    @field_validator("probabilities")
    @classmethod
    def probabilities_must_sum_to_100(cls, v):
        total = sum(v)
        if abs(total - 100) > 0.01:
            raise ValueError(f"Probabilities must sum to 100%, got {total}%")
        return v

    @field_validator("probabilities")
    @classmethod
    def all_probabilities_must_be_positive(cls, v):
        if any(p <= 0 for p in v):
            raise ValueError("All probabilities must be positive")
        return v

    @field_validator("values")
    @classmethod
    def values_must_match_probabilities(cls, v, info):
        if "probabilities" in info.data and len(v) != len(info.data["probabilities"]):
            raise ValueError("Values array length must match probabilities")
        return v
