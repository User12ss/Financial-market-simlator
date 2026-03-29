"""Data models for policy scenarios and simulation inputs."""

from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field


class PolicyType(str, Enum):
    MONETARY = "monetary"
    FISCAL = "fiscal"
    TRADE = "trade"
    REGULATORY = "regulatory"
    SUPPLY_SHOCK = "supply_shock"
    MIXED = "mixed"


class Scenario(BaseModel):
    """A parsed policy scenario ready for simulation."""

    raw_input: str = Field(description="Original user input")
    policy_type: PolicyType = Field(default=PolicyType.MIXED, description="Classified policy type")
    description: str = Field(default="", description="Parsed policy description in precise economic terms")
    magnitude: str = Field(default="moderate", description="Estimated magnitude: mild, moderate, severe")
    is_surprise: bool = Field(default=True, description="Whether the policy is unexpected by markets")
    base_economy: str = Field(default="US", description="Target economy")
    baseline_conditions: str = Field(default="", description="Assumed starting macro conditions")
    time_horizon: str = Field(default="12 months", description="Simulation time horizon")

    @classmethod
    def from_raw(cls, user_input: str, base_economy: str = "US") -> Scenario:
        """Create a scenario from raw user input. Fields will be filled by the orchestrator."""
        return cls(raw_input=user_input, base_economy=base_economy)
