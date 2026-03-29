"""Equity Market Agent — stock market and corporate sector specialist."""

from agents.base import BaseAgent


class EquityMarketAgent(BaseAgent):
    id = "equity_market"
    name = "Equity Market Agent"
    domain = "Stock market behavior, sector analysis, corporate earnings, valuations, risk appetite"
    outputs = "Sector rotation signals, P/E direction, earnings revision estimates, market regime"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:
- Evaluate the impact on corporate earnings expectations across major sectors (tech, financials, energy, consumer, healthcare, industrials, utilities).
- Assess whether the policy triggers P/E expansion (risk-on, lower rates) or compression (risk-off, higher rates, uncertainty).
- Model sector rotation: which sectors benefit and which suffer from this specific policy?
- Consider the discount rate effect on growth vs. value stocks.
- Evaluate impact on buyback activity, dividend policy, and capital allocation decisions.
- Assess whether the policy changes the market regime (bull, bear, range-bound, crisis).
- Factor in positioning and flow dynamics: forced selling, margin calls, passive rebalancing."""
