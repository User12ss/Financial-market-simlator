"""Currency & Forex Agent — exchange rate and capital flow specialist."""

from agents.base import BaseAgent


class ForexAgent(BaseAgent):
    id = "forex"
    name = "Currency & Forex Agent"
    domain = "Exchange rates, interest rate differentials, capital flows, trade balance, carry trade"
    outputs = "Currency direction and magnitude, capital flow shifts, trade competitiveness"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:
- Evaluate interest rate differentials: does the policy widen or narrow the gap vs. major counterparts?
- Model capital flow effects: does the policy attract or repel foreign capital (portfolio flows and FDI)?
- Assess trade balance implications: does the resulting currency move help or hurt export competitiveness?
- Consider carry trade dynamics: does the policy make the currency more attractive for yield-seeking flows?
- Evaluate safe-haven flows: does the policy increase or decrease global risk aversion affecting USD/JPY/CHF/gold?
- Model central bank intervention risk if the currency move becomes disorderly.
- For non-US scenarios, assess the bilateral and effective exchange rate implications separately."""
