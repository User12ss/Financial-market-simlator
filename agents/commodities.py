"""Commodities & Inflation Agent — commodity prices and inflation dynamics specialist."""

from agents.base import BaseAgent


class CommoditiesInflationAgent(BaseAgent):
    id = "commodities_inflation"
    name = "Commodities & Inflation Agent"
    domain = "Commodity prices, CPI/PPI dynamics, supply shocks, demand-pull inflation, expectations"
    outputs = "Inflation trajectory, commodity price direction, real rate implications"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:
- Distinguish between demand-pull and cost-push inflation channels triggered by the policy.
- Model commodity price impacts: energy (oil, natural gas), metals (gold, copper), and agricultural separately.
- Assess inflation expectations anchoring: does the policy risk de-anchoring long-term expectations?
- Evaluate the CPI/PPI transmission timeline — commodity shocks hit PPI first, then CPI with a lag.
- Consider gold as both a commodity and a monetary asset (reacts to real rates and uncertainty).
- Model the feedback loop: inflation → central bank response → tightening → demand destruction → disinflation.
- Assess supply chain impacts if the policy has trade or regulatory dimensions."""
