"""Fixed Income Agent — bond market and yield curve specialist."""

from agents.base import BaseAgent


class FixedIncomeAgent(BaseAgent):
    id = "fixed_income"
    name = "Fixed Income Agent"
    domain = "Bond markets, yield curve, credit spreads, sovereign debt, duration risk"
    outputs = "Yield curve shape changes, spread direction, bond demand/supply dynamics"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:
- Model the yield curve response: parallel shift, steepening, flattening, or inversion.
- Differentiate between short-end (rate expectations) and long-end (term premium, inflation expectations) drivers.
- Assess credit spread movements: investment grade vs. high yield, and whether risk repricing is warranted.
- Evaluate sovereign debt dynamics: issuance changes, foreign demand shifts, reserve manager behavior.
- Consider the impact on mortgage rates and the housing channel.
- Model duration risk: who holds the duration exposure and could forced selling amplify moves?
- Assess real yield changes (nominal yield minus inflation expectations) and their cross-asset implications."""
