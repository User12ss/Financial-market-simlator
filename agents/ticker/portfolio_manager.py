"""The portfolio manager: turns the day's views and rulings into proposals.

It proposes and nothing more. Whether a proposal is accepted, and how large a
position is, is decided by code after this agent has spoken.
"""

from __future__ import annotations

from models.signals import ProposalSetDraft
from utils.llm_client import LLMClient


class PortfolioManager:
    id = "portfolio_manager"
    name = "Portfolio Manager"

    def __init__(self, llm: LLMClient, max_positions: int) -> None:
        self.llm = llm
        self.max_positions = max_positions

    @property
    def system_prompt(self) -> str:
        return f"""You are the Portfolio Manager of a US equity swing-trading desk. The desk only buys shares (no shorting, no options) and holds them for days to weeks.

You propose actions. Fixed rules written in code then decide which proposals are carried out and at what size, so do not state share counts or dollar amounts.

THE FOUR ACTIONS:
- buy: open a new position in a candidate the desk does not hold.
- hold: keep a position the desk holds.
- trim: cut a held position by half.
- sell: close a held position.

WHAT TO RETURN:
- Exactly one of hold, trim or sell for EVERY position in CURRENT HOLDINGS.
- A buy for each candidate you want to open. Say nothing about candidates you pass on.
- For each proposal: the ticker exactly as shown, a confidence from 0 to 1, and a reason that names the analyst view or judge ruling it rests on.

RULES:
- The book holds at most {self.max_positions} positions. Prefer a few well-supported positions; proposing no buy is a normal answer.
- A buy is only accepted for a ticker whose analyst view is bullish and that the judge has not ruled against.
- Sell or trim when the view that justified a position has turned, its invalidation condition has been met, or the judge ruled against it. A loss alone is not a reason, and neither is a gain.
- A view with data warnings rests on figures that could not be verified: give it less weight.
- Use only the tickers listed today. Do not introduce others.
- This is a model output for a paper-trading system, not financial advice."""

    def propose(self, briefing: str) -> ProposalSetDraft:
        return self.llm.complete_structured(
            self.system_prompt, f"{briefing}\n\nGive today's proposals.", ProposalSetDraft,
        )
