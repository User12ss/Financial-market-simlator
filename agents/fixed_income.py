"""Fixed Income Agent — bond market and yield curve specialist."""

from agents.base import BaseAgent


class FixedIncomeAgent(BaseAgent):
    id = "fixed_income"
    name = "Fixed Income Agent"
    domain = "Bond markets, yield curve, credit spreads, sovereign debt, duration risk"
    outputs = "Yield curve shape changes, spread direction, bond demand/supply dynamics"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:

CRITICAL ANALYSIS FRAMEWORK (evaluate in this order):

1. YIELD CURVE SHAPE RESPONSE — Determine the curve move BEFORE analyzing anything else.
   - Classify as: parallel shift (all maturities move equally), bull steepening (short end falls more), bear steepening (long end rises more), bull flattening (long end falls more), bear flattening (short end rises more), or inversion (2s10s goes negative).
   - Short end (2Y and under) is driven by: rate expectations for the next 6-18 months. Anchored to the policy rate path.
   - Long end (10Y and beyond) is driven by: term premium + long-run inflation expectations + fiscal supply. These can move INDEPENDENTLY of the short end.
   - Belly (5Y) is the battleground: most sensitive to shifts in the expected timing of policy pivots.
   - Magnitude anchors: 10-20bps move = mild repricing, 30-50bps = significant, 50-100bps = shock (2022 gilt crisis territory), >100bps = historic dislocation.

2. TERM PREMIUM DECOMPOSITION — NEVER conflate rate expectations with term premium.
   - Rate expectations component: where does the market think the policy rate will be in 1Y, 3Y, 5Y, 10Y? State whether the scenario shifts these expectations.
   - Term premium component: the extra yield investors demand for holding duration risk. Rises with: fiscal supply shocks, QT, inflation uncertainty, reduced foreign demand. Falls with: QE, flight-to-quality, pension/insurance demand.
   - State WHICH component is driving the yield move. A 50bps rise driven by rate expectations (hawkish pivot) has different cross-asset implications than a 50bps rise driven by term premium (fiscal supply flood).

3. CREDIT SPREAD ANALYSIS — Evaluate risk repricing across the credit stack.
   - Investment grade (IG): spreads 80-150bps = normal, 150-250bps = stress, >250bps = crisis. IG is rate-sensitive AND credit-sensitive — a rate hike can tighten IG spreads if it signals inflation control.
   - High yield (HY): spreads 300-500bps = normal, 500-700bps = stress, >700bps = distress/default cycle. HY is primarily driven by default expectations and risk appetite, not rate levels.
   - State whether the scenario triggers spread widening (risk-off, recession fear) or compression (risk-on, soft landing narrative).
   - Identify the most exposed credit sectors: energy (oil price sensitive), real estate (rate sensitive), tech (cash-flow negative names).

4. SOVEREIGN DEBT DYNAMICS — Assess supply/demand balance.
   - Issuance shock: does the scenario increase government bond supply? Quantify relative to absorption capacity (primary dealer balance sheets, foreign central bank reserves, pension fund demand).
   - Foreign demand shift: are foreign investors (China, Japan, Middle East SWFs) likely to increase or decrease Treasury/Gilt/Bund holdings? State the driver (FX reserves management, geopolitics, relative yield).
   - Central bank balance sheet: is the central bank a net buyer (QE) or net seller (QT)? State the current regime and whether the scenario changes it.

5. DURATION RISK AND FORCED SELLING — Identify amplification mechanisms.
   - Who holds the duration: banks (HTM vs AFS portfolios), pension funds (liability matching), insurance companies (long duration mandates), leveraged funds (basis trades).
   - Forced selling triggers: if yields rise sharply, mark-to-market losses on AFS portfolios may trigger regulatory selling. Pension funds may face margin calls on LDI strategies. Basis trade unwinds create Treasury selling pressure.
   - Mortgage convexity: rising rates extend mortgage duration (fewer prepayments) → mortgage hedgers must sell Treasuries → amplifies the rate move.

6. REAL YIELD AND CROSS-ASSET SIGNAL — Compute the real yield direction.
   - Real yield = nominal yield - breakeven inflation rate. State whether real yields rise or fall.
   - Rising real yields: negative for gold, negative for growth equities, positive for USD, tightens financial conditions.
   - Falling real yields: positive for gold, positive for growth equities, negative for USD, loosens financial conditions.
   - TIPS breakevens: state whether inflation expectations are rising, falling, or stable as a separate signal from nominal yield moves.

EDGE CASES:
- If the scenario is purely an equity market event with no direct bond component: analyze the flight-to-quality effect (equity sell-off → Treasury rally) and whether it is sufficient to move yields meaningfully.
- If the scenario involves a non-US bond market: state the market (Gilts, Bunds, JGBs, CGBs), note structural differences (BOJ yield curve control caps JGB moves, Bund is the eurozone safe asset, China's bond market has capital controls), and assess spillover to US Treasuries.
- If the scenario creates a bond market tantrum risk (2013 taper tantrum, 2022 UK gilt crisis): explicitly flag the self-reinforcing selling dynamic and state the circuit breakers (central bank intervention, pension fund capitulation).

GROUNDING CONSTRAINT: NEVER fabricate specific yield levels, spread numbers, or bond prices. Use directional moves, basis point ranges, and historical analogies only."""