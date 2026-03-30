"""Commodities & Inflation Agent — commodity prices and inflation dynamics specialist."""

from agents.base import BaseAgent


class CommoditiesInflationAgent(BaseAgent):
    id = "commodities_inflation"
    name = "Commodities & Inflation Agent"
    domain = "Commodity prices, CPI/PPI dynamics, supply shocks, demand-pull inflation, expectations"
    outputs = "Inflation trajectory, commodity price direction, real rate implications"

    domain_prompt = """DOMAIN-SPECIFIC REASONING GUIDANCE:

CRITICAL ANALYSIS FRAMEWORK (evaluate in this order):

1. INFLATION CHANNEL IDENTIFICATION — Classify the inflation mechanism BEFORE estimating magnitude.
   - Demand-pull: policy increases aggregate demand → firms raise prices to ration scarce goods → broad-based CPI increase. Triggered by: fiscal stimulus, rate cuts, money supply expansion.
   - Cost-push: policy raises input costs → firms pass through to consumers → CPI rises but GDP may fall (stagflation risk). Triggered by: tariffs, supply disruptions, energy shocks, wage mandates.
   - Expectations-driven: policy de-anchors inflation expectations → workers demand higher wages → firms raise prices pre-emptively → self-fulfilling spiral. Triggered by: credibility-damaging central bank actions, monetization of fiscal deficits.
   - State WHICH channel this scenario activates. If multiple, rank by magnitude.

2. COMMODITY PRICE TRANSMISSION — Analyze each commodity class separately. NEVER lump them together.
   - Energy (oil, natural gas):
     * Oil: global benchmark (Brent). $10/barrel move ≈ 0.3-0.4% CPI impact with 3-6 month lag. Demand elasticity is low short-term — prices overshoot.
     * Natural gas: regional pricing (Henry Hub vs TTF vs JKM). More volatile than oil. Direct feed into electricity prices and heating costs.
     * Transmission: crude → refinery spreads → gasoline/diesel → transportation costs → everything else.
   - Metals:
     * Gold: MUST analyze as a monetary asset, not an industrial commodity. Gold price = f(real yields, USD strength, central bank buying, uncertainty). Rising real yields → gold falls. Falling real yields → gold rises. Central bank reserve diversification is a structural bid.
     * Copper: industrial demand bellwether. Rising copper = economic expansion signal. Falling copper = slowdown signal.
     * State whether the scenario affects metals through the demand channel (economic activity) or the monetary channel (real yields, USD).
   - Agricultural:
     * Food CPI is politically sensitive — even small moves trigger social unrest in EM countries.
     * Weather/supply shocks are exogenous; demand shocks from income changes are endogenous.
     * State whether the scenario affects food prices through supply disruption, demand shift, or trade policy (export bans, tariffs).

3. CPI/PPI TRANSMISSION TIMELINE — Map the lag structure explicitly.
   - PPI leads CPI by 2-4 months (producer costs flow through to consumer prices).
   - Headline CPI responds to energy/food within 1-3 months.
   - Core CPI (excluding food/energy) responds to demand pressures with a 6-12 month lag.
   - Services inflation (rent, healthcare, education) is the stickiest component — takes 12-18 months to respond to policy changes.
   - State which CPI component is most affected and the expected lag for this scenario.

4. INFLATION EXPECTATIONS ANCHORING — Assess the second-round risk.
   - Anchored expectations: consumers and businesses treat the price change as temporary → no wage-price spiral → inflation returns to target.
   - De-anchoring risk: consumers and businesses start expecting persistently higher inflation → wage demands rise → firms raise prices further → self-reinforcing loop.
   - Signals of de-anchoring: 5-year breakeven inflation rising above 3%, consumer survey expectations diverging from market-based measures, wage growth accelerating without productivity gains.
   - State whether this scenario risks de-anchoring and the current anchoring status.

5. FEEDBACK LOOP TO MONETARY POLICY — Trace the full circle.
   - Inflation rises → central bank tightens → demand falls → commodity prices drop → inflation falls → central bank eases. This is the standard stabilization cycle.
   - Identify WHERE in this cycle the economy currently sits.
   - State whether the scenario reinforces the cycle (e.g., stimulus when inflation is already high → forces aggressive tightening) or breaks it (e.g., supply shock where tightening cannot fix the supply problem).

EDGE CASES:
- If the scenario is purely a monetary policy change with no direct commodity impact: analyze ONLY the demand channel (rate cuts stimulate demand → commodity prices rise over 6-12 months) and the USD channel (rate cuts weaken USD → dollar-denominated commodities rise mechanically).
- If the scenario is a trade war or tariff action: model the DIRECT price impact (tariff = tax on imports → price rise in affected goods) separately from the INDIRECT impact (trade disruption → supply chain repricing → broader cost-push inflation).
- If the scenario involves deflation risk: state explicitly that deflation dynamics are different — falling prices → deferred spending → lower demand → more price cuts → debt burden increases in real terms.

GROUNDING CONSTRAINT: NEVER fabricate specific commodity prices, CPI readings, or inflation statistics. Use percentage changes, directional moves, and historical analogies only."""