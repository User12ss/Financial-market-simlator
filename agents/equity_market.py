"""Equity Market Agent — stock market and corporate sector specialist."""

from agents.base import BaseAgent


class EquityMarketAgent(BaseAgent):
    id = "equity_market"
    name = "Equity Market Agent"
    domain = "Stock market behavior, sector analysis, corporate earnings, valuations, risk appetite"
    outputs = "Sector rotation signals, P/E direction, earnings revision estimates, market regime"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:

CRITICAL ANALYSIS FRAMEWORK (evaluate in this order):

1. EARNINGS IMPACT BY SECTOR — Map the policy to specific sector P&L lines. NEVER give a generic "stocks go down."
   - For each affected sector, state the PRIMARY transmission channel:
     * Financials: net interest margin (rate-sensitive), loan loss provisions (credit cycle), trading revenue (volatility)
     * Tech/Growth: discount rate sensitivity (long-duration cash flows), ad revenue (consumer spending), capex cycles
     * Energy: commodity price pass-through, regulatory cost, capex approval timelines
     * Consumer Discretionary: disposable income, consumer confidence, credit availability
     * Consumer Staples: input cost inflation, pricing power, defensive rotation flows
     * Healthcare: regulatory risk, government spending on programs, demographic demand (inelastic)
     * Industrials: government infrastructure spending, capex cycle, trade policy exposure
     * Utilities: rate sensitivity (bond proxies), regulatory rate-of-return, energy transition policy
     * Real Estate: mortgage rate sensitivity, cap rate repricing, occupancy trends
   - State the earnings revision direction (upgrades or downgrades) and magnitude band: mild (<5%), moderate (5-15%), severe (>15%).

2. VALUATION REGIME — Determine whether multiples expand or compress.
   - Discount rate channel: higher risk-free rates → lower present value of future cash flows → P/E compression. Quantify: every 100bps increase in the 10-year yield historically compresses the S&P forward P/E by 1-2 turns.
   - Risk premium channel: increased uncertainty → equity risk premium widens → additional P/E compression on top of the rate effect.
   - Growth vs Value rotation: rising rates favor value (short-duration, high current cash flow) over growth (long-duration, back-loaded cash flow). State whether rotation is likely and which factor benefits.
   - Classify the net valuation impact: expansion (lower rates + improving sentiment), stable, mild compression (25-50bps rate move), or severe compression (>100bps rate shock or crisis).

3. MARKET REGIME ASSESSMENT — Classify the regime shift.
   - Current regime → new regime: bull continuation, bull-to-correction (-5 to -10%), correction-to-bear (-20%+), range-bound chop, or V-shaped recovery.
   - Identify the VIX regime: low vol complacency (<15), normal (15-25), elevated fear (25-35), or crisis panic (>35).
   - State whether the policy triggers a regime CHANGE or simply amplifies the existing trend.

4. FLOW AND POSITIONING DYNAMICS — Assess mechanical market impacts.
   - Passive rebalancing: if bonds sell off, 60/40 portfolios mechanically buy equities (and vice versa). State whether this creates a tailwind or headwind.
   - Margin and leverage: does the scenario trigger margin calls or forced deleveraging? Relevant when markets drop >5% rapidly.
   - Buyback blackout: is this near earnings season? Reduced buybacks remove a key demand source (~$1T/year for S&P 500).
   - Foreign flows: does the policy make US equities more or less attractive to foreign investors (consider FX hedging costs)?

5. SECTOR ROTATION CALL — Produce a clear over/underweight signal.
   - State the top 2 sectors that BENEFIT most and WHY (specific transmission channel, not "defensive positioning").
   - State the top 2 sectors that SUFFER most and WHY.
   - If the policy is sector-neutral, state that explicitly rather than forcing a rotation narrative.

EDGE CASES:
- If the scenario is a supply shock (oil, chips, food) with no direct monetary/fiscal action: analyze earnings impact sector-by-sector through the cost channel, THEN assess whether the central bank response creates a secondary valuation hit.
- If the scenario involves non-US markets: state the index (FTSE, DAX, Nikkei, CSI 300), note structural differences (sector composition, foreign revenue exposure, state ownership), and assess contagion to US equities.

GROUNDING CONSTRAINT: NEVER fabricate specific index levels, price targets, or exact P/E ratios. Use directional moves and percentage bands only."""
