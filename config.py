"""Configuration for the financial market simulation framework."""

import os
from dotenv import load_dotenv

load_dotenv()

# --- API ---
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
MODEL_NAME: str = os.getenv("MODEL_NAME", "claude-sonnet-4-20250514")
MODEL_FAST: str = os.getenv("MODEL_FAST", "claude-haiku-4-5-20251001")
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))

# --- Simulation Defaults ---
BASE_ECONOMY: str = os.getenv("BASE_ECONOMY", "US")
TIME_HORIZONS: list[str] = ["immediate (0-1 week)", "short-term (1-3 months)", "medium-term (3-12 months)"]

# --- Token Budgets (Optimization 3: right-size each call) ---
TOKEN_BUDGET: dict[str, int] = {
    "tier1": 2048,
    "tier2": 1536,
    "synthesis_draft": 4096,
    "synthesis_critic": 2048,
    "scenario_parse": 1024,
    "comparison": 4096,
}

# --- Agent Tiers (Optimization 1: upstream agents produce signals first) ---
AGENT_TIERS: dict[str, list[str]] = {
    "tier1": ["central_bank", "fiscal_policy", "commodities_inflation"],
    "tier2": ["fixed_income", "equity_market", "forex", "sentiment"],
}

# --- Agent Routing by Policy Type (Optimization 2: selective activation) ---
AGENT_ROUTING: dict[str, dict[str, list[str]]] = {
    "monetary": {
        "tier1": ["central_bank", "commodities_inflation"],
        "tier2": ["fixed_income", "forex", "equity_market", "sentiment"],
        "skip": ["fiscal_policy"],
    },
    "fiscal": {
        "tier1": ["fiscal_policy", "central_bank"],
        "tier2": ["fixed_income", "equity_market", "sentiment"],
        "skip": ["forex", "commodities_inflation"],
    },
    "trade": {
        "tier1": ["commodities_inflation", "fiscal_policy"],
        "tier2": ["forex", "equity_market", "fixed_income", "sentiment"],
        "skip": [],
    },
    "regulatory": {
        "tier1": ["fiscal_policy"],
        "tier2": ["equity_market", "fixed_income", "sentiment"],
        "skip": ["central_bank", "forex", "commodities_inflation"],
    },
    "supply_shock": {
        "tier1": ["commodities_inflation", "central_bank"],
        "tier2": ["equity_market", "fixed_income", "forex", "sentiment"],
        "skip": ["fiscal_policy"],
    },
    "mixed": {
        "tier1": ["central_bank", "fiscal_policy", "commodities_inflation"],
        "tier2": ["fixed_income", "equity_market", "forex", "sentiment"],
        "skip": [],
    },
}

# --- Agent Definitions ---
AGENT_ROSTER: list[dict] = [
    {
        "id": "central_bank",
        "name": "Central Bank Agent",
        "domain": "Monetary policy, interest rates, money supply, QE/QT, reserve requirements, forward guidance",
        "outputs": "Rate path expectations, liquidity conditions, banking system stress indicators",
    },
    {
        "id": "fiscal_policy",
        "name": "Fiscal Policy Agent",
        "domain": "Government spending, taxation, budget deficits/surpluses, stimulus, austerity, public debt",
        "outputs": "Aggregate demand impact, employment effects, government bond supply, debt sustainability",
    },
    {
        "id": "equity_market",
        "name": "Equity Market Agent",
        "domain": "Stock market behavior, sector analysis, corporate earnings, valuations, risk appetite",
        "outputs": "Sector rotation signals, P/E direction, earnings revision estimates, market regime",
    },
    {
        "id": "fixed_income",
        "name": "Fixed Income Agent",
        "domain": "Bond markets, yield curve, credit spreads, sovereign debt, duration risk",
        "outputs": "Yield curve shape changes, spread direction, bond demand/supply dynamics",
    },
    {
        "id": "forex",
        "name": "Currency & Forex Agent",
        "domain": "Exchange rates, interest rate differentials, capital flows, trade balance, carry trade",
        "outputs": "Currency direction and magnitude, capital flow shifts, trade competitiveness",
    },
    {
        "id": "commodities_inflation",
        "name": "Commodities & Inflation Agent",
        "domain": "Commodity prices, CPI/PPI dynamics, supply shocks, demand-pull inflation, expectations",
        "outputs": "Inflation trajectory, commodity price direction, real rate implications",
    },
    {
        "id": "sentiment",
        "name": "Sentiment & Behavioral Agent",
        "domain": "Market psychology, investor confidence, risk-on/risk-off, herding, expectation anchoring",
        "outputs": "Sentiment regime, positioning shifts, volatility expectations, narrative momentum",
    },
]
