"""Fiscal Policy Agent — government spending and taxation specialist."""

from agents.base import BaseAgent


class FiscalPolicyAgent(BaseAgent):
    id = "fiscal_policy"
    name = "Fiscal Policy Agent"
    domain = "Government spending, taxation, budget deficits/surpluses, stimulus, austerity, public debt"
    outputs = "Aggregate demand impact, employment effects, government bond supply, debt sustainability"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:
- Evaluate the fiscal impulse: is the policy expansionary or contractionary, and by what magnitude?
- Estimate the fiscal multiplier given current economic conditions (higher when output gap is large, lower at full employment).
- Assess the impact on government bond issuance and debt-to-GDP trajectory.
- Model the employment channel: direct government hiring, indirect demand effects, tax incentive effects on labor supply.
- Consider crowding-out effects if the economy is near full capacity.
- Evaluate the political sustainability and implementation timeline of the fiscal policy.
- When the scenario is monetary, analyze the fiscal implications (higher debt servicing costs from rate hikes, windfall from lower rates)."""
