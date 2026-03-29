"""Central Bank Agent — monetary policy specialist."""

from agents.base import BaseAgent


class CentralBankAgent(BaseAgent):
    id = "central_bank"
    name = "Central Bank Agent"
    domain = "Monetary policy, interest rates, money supply, QE/QT, reserve requirements, forward guidance"
    outputs = "Rate path expectations, liquidity conditions, banking system stress indicators"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:
- Evaluate how the scenario changes the expected rate path (higher for longer, pivot, emergency cut).
- Assess the impact on money supply and credit conditions in the banking system.
- Consider the central bank's reaction function: would this scenario force a policy response?
- Model the transmission mechanism: policy rate → interbank rates → lending rates → credit availability.
- Factor in forward guidance: how would communication change alongside the policy action?
- Consider reserve requirements, discount window usage, and banking system liquidity stress.
- When the scenario is fiscal rather than monetary, analyze whether it forces the central bank's hand (fiscal dominance)."""
