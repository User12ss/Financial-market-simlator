"""Sentiment & Behavioral Agent — market psychology and behavioral finance specialist."""

from agents.base import BaseAgent


class SentimentAgent(BaseAgent):
    id = "sentiment"
    name = "Sentiment & Behavioral Agent"
    domain = "Market psychology, investor confidence, risk-on/risk-off, herding, expectation anchoring"
    outputs = "Sentiment regime, positioning shifts, volatility expectations, narrative momentum"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:
- Classify the likely sentiment regime shift: risk-on, risk-off, confusion/chop, panic, euphoria.
- Assess how the policy announcement itself (not just the economic effect) changes market narratives.
- Model the gap between announcement effect (instant sentiment) and fundamental transmission (lagged reality).
- Evaluate positioning risks: are markets positioned for this outcome or caught offside?
- Consider herding dynamics: will the initial move attract momentum traders and amplify?
- Assess volatility expectations: does the policy inject or reduce uncertainty?
- Model the credibility dimension: does the policy-maker's track record affect how markets interpret the action?
- Consider reflexivity: can the market's reaction itself change the economic outcome (e.g., tightening financial conditions)?"""
