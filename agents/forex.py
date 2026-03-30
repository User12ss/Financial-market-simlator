"""Currency & Forex Agent — exchange rate and capital flow specialist."""

from agents.base import BaseAgent


class ForexAgent(BaseAgent):
    id = "forex"
    name = "Currency & Forex Agent"
    domain = "Exchange rates, interest rate differentials, capital flows, trade balance, carry trade"
    outputs = "Currency direction and magnitude, capital flow shifts, trade competitiveness"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:

CRITICAL ANALYSIS FRAMEWORK (evaluate in this order):

1. INTEREST RATE DIFFERENTIAL — The dominant short-term FX driver. Evaluate first.
   - Identify the rate differential shift: does the scenario widen or narrow the gap between the domestic policy rate and the rates of major counterparts (Fed vs ECB, Fed vs BOJ, Fed vs BOE)?
   - Direction rule: widening rate differential in favor of currency X → X appreciates (capital flows toward higher yield).
   - Magnitude anchors: 25bps differential change = 0.5-1% FX move, 50bps = 1-3%, 100bps+ = 3-5%+ (for major pairs). EM currencies are 2-3x more sensitive.
   - State the EXPECTED vs ACTUAL differential: if the rate move was priced in, the FX impact is near zero. Only the surprise component moves FX.

2. CAPITAL FLOW CHANNELS — Decompose into three distinct flows:
   - Portfolio flows (hot money): bond investors and equity investors reallocating based on relative returns. These move FAST (days to weeks), are the primary short-term FX driver, and reverse quickly.
   - FDI flows (sticky money): multinational capex decisions, factory relocation, M&A. These move SLOWLY (quarters to years) but create durable FX demand. Relevant for trade policy and regulatory scenarios.
   - Reserve flows (institutional): central bank FX reserve management, sovereign wealth fund rebalancing. These are large but infrequent. Relevant when a scenario threatens reserve currency status or triggers reserve diversification.
   - State which channel dominates for THIS specific scenario and the expected time lag.

3. CARRY TRADE DYNAMICS — Evaluate the yield-seeking flow impact.
   - Carry trade = borrow in low-yield currency, invest in high-yield currency. Profitable when FX is stable, destroyed when volatility spikes.
   - If the scenario raises rates in a high-yield currency: carry flows increase → currency appreciates further → self-reinforcing loop.
   - If the scenario triggers a volatility spike: carry trades unwind → high-yield currencies sell off sharply, funding currencies (JPY, CHF, EUR) rally. This is the "carry crash" dynamic.
   - Anchor: JPY is the dominant funding currency. A BOJ rate hike or global risk-off event triggers massive carry unwind → JPY rallies sharply.

4. TRADE BALANCE AND COMPETITIVENESS — Evaluate the real economy FX channel.
   - Currency appreciation → exports become expensive → trade balance worsens → medium-term drag on currency (self-correcting).
   - Currency depreciation → exports become competitive → trade balance improves → medium-term support for currency.
   - This channel operates on a 6-18 month lag (J-curve effect: trade balance initially WORSENS after depreciation before improving).
   - State whether the scenario's FX impact will trigger a competitiveness response, and over what time horizon.

5. SAFE-HAVEN AND RISK SENTIMENT — Evaluate the "fear channel."
   - Risk-off flows: capital moves to USD, JPY, CHF, gold. EM currencies and commodity currencies (AUD, CAD, NOK) sell off.
   - Risk-on flows: capital moves to EM, commodity currencies, high-yield FX. Safe havens weaken.
   - Classify the scenario's risk sentiment impact: strong risk-off, mild risk-off, neutral, mild risk-on, strong risk-on.
   - USD has a DUAL role: it strengthens in both "US economy outperforms" (rate differential) AND "global crisis" (safe haven). State which driver applies.

6. INTERVENTION AND POLICY RESPONSE — Assess whether authorities will resist the FX move.
   - Verbal intervention threshold: when officials start jawboning (typically after 5-10% move in a major currency).
   - Actual intervention threshold: when central banks spend reserves to defend a level (rare for majors, common for EM).
   - Capital controls: relevant for China (PBOC manages CNY tightly), some EM countries. State if the scenario makes controls more or less likely.
   - State whether intervention risk caps the FX move's magnitude.

EDGE CASES:
- If the scenario is purely domestic (e.g., US fiscal policy) with no explicit FX component: analyze the INDIRECT FX effect through rate differentials and risk sentiment. Every macro policy has an FX consequence.
- If the scenario involves an EM currency: note the additional risks — FX-denominated debt burden, thin liquidity, political instability premium, commodity dependence — that amplify moves beyond what rate differentials alone would predict.
- If the scenario involves competitive devaluation or currency war dynamics: flag the retaliation risk and model the tit-for-tat escalation path.

GROUNDING CONSTRAINT: NEVER fabricate specific exchange rates, pip levels, or positioning data. Use percentage move ranges and directional language only."""