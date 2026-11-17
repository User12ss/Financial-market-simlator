"""Configuration for the financial market simulation framework."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent

# --- LLM providers ---
# "ollama"        local open-source models through Ollama
# "openai_compat" any hosted service exposing the OpenAI chat-completions API
# "anthropic"     Claude through the official SDK
#
# Two tiers are configured: the main model does the analysis, the fast model
# does cheap parsing. Each tier can use a different provider.
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "ollama": {"model": "qwen2.5-coder:7b", "fast": "llama3.2:latest"},
    "openai_compat": {"model": "", "fast": ""},
    "anthropic": {"model": "claude-sonnet-5-5", "fast": "claude-haiku-5-5"},
}


def provider_default(provider: str, key: str) -> str:
    """Default model name for a provider tier ("model" or "fast")."""
    return _PROVIDER_DEFAULTS.get(provider, {}).get(key, "")


LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
LLM_MODEL: str = os.getenv("LLM_MODEL", "").strip() or provider_default(LLM_PROVIDER, "model")
LLM_PROVIDER_FAST: str = os.getenv("LLM_PROVIDER_FAST", "").strip().lower() or LLM_PROVIDER
LLM_MODEL_FAST: str = (
    os.getenv("LLM_MODEL_FAST", "").strip()
    or provider_default(LLM_PROVIDER_FAST, "fast")
    or LLM_MODEL
)

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OPENAI_COMPAT_BASE_URL: str = os.getenv("OPENAI_COMPAT_BASE_URL", "").rstrip("/")
OPENAI_COMPAT_API_KEY: str = os.getenv("OPENAI_COMPAT_API_KEY", "")

MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
# Extra attempts when a reply does not validate against the requested schema.
LLM_SCHEMA_RETRIES: int = int(os.getenv("LLM_SCHEMA_RETRIES", "2"))
LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "900"))
# Sampling temperature for open-source providers. Never sent to Claude:
# current Claude models reject non-default sampling parameters.
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.3"))

# Ollama truncates prompts longer than the context window without any error,
# so the client refuses prompts that would not fit in OLLAMA_NUM_CTX.
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT: int = int(os.getenv("OLLAMA_NUM_PREDICT", "4096"))
# A local machine runs one generation at a time; extra requests wait their turn.
OLLAMA_MAX_PARALLEL: int = int(os.getenv("OLLAMA_MAX_PARALLEL", "1"))

# Output cap for Claude. Thinking counts against it, so it must stay generous.
ANTHROPIC_MAX_TOKENS: int = int(os.getenv("ANTHROPIC_MAX_TOKENS", "16000"))

# --- Simulation Defaults ---
BASE_ECONOMY: str = os.getenv("BASE_ECONOMY", "US")

# --- Orchestrator Configuration ---
MAX_ORCHESTRATOR_ITERATIONS: int = int(os.getenv("MAX_ORCHESTRATOR_ITERATIONS", "25"))

# --- Debate Configuration ---
MAX_DEBATE_ROUNDS: int = int(os.getenv("MAX_DEBATE_ROUNDS", "2"))

# --- Memory Configuration ---
MEMORY_ENABLED: bool = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
MEMORY_DIR: str = os.getenv("MEMORY_DIR", ".memory")
MEMORY_RETRIEVE_K: int = int(os.getenv("MEMORY_RETRIEVE_K", "3"))

# --- Market Data ---
# Prices, fundamentals and headlines come from Yahoo Finance, macro series
# from FRED. Both are free and need no key. Every fetch is saved as a
# point-in-time snapshot under SNAPSHOT_DIR.
MARKET_DATA_ENABLED: bool = os.getenv("MARKET_DATA_ENABLED", "true").lower() == "true"
SNAPSHOT_DIR: str = os.getenv("SNAPSHOT_DIR", "").strip() or str(PROJECT_ROOT / "data_store" / "snapshots")

# --- Daily trading pipeline (shadow mode: signals only, no orders) ---
# Every number here is a placeholder until the user sets it.
JOURNAL_PATH: str = os.getenv("JOURNAL_PATH", "").strip() or str(PROJECT_ROOT / "data_store" / "journal.sqlite3")
RUNS_DIR: str = os.getenv("RUNS_DIR", "").strip() or str(PROJECT_ROOT / "data_store" / "runs")
# One symbol per line. Empty means the built-in list in pipeline/universe.py.
UNIVERSE_FILE: str = os.getenv("UNIVERSE_FILE", "").strip()

# Candidate screen: plain rules, no model involved.
PIPELINE_MAX_CANDIDATES: int = int(os.getenv("PIPELINE_MAX_CANDIDATES", "10"))
PIPELINE_MIN_PRICE: float = float(os.getenv("PIPELINE_MIN_PRICE", "5"))
PIPELINE_MIN_DOLLAR_VOLUME: float = float(os.getenv("PIPELINE_MIN_DOLLAR_VOLUME", "20000000"))

# The macro team's view is reused until it is this many days old.
PIPELINE_MACRO_MAX_AGE_DAYS: int = int(os.getenv("PIPELINE_MACRO_MAX_AGE_DAYS", "7"))
# How many of the strongest new candidates get a bull/bear debate, and how many rounds each.
PIPELINE_DEBATE_TOP: int = int(os.getenv("PIPELINE_DEBATE_TOP", "3"))
PIPELINE_DEBATE_ROUNDS: int = int(os.getenv("PIPELINE_DEBATE_ROUNDS", "1"))
# A run stops, and proposes nothing, once its model calls have cost this much.
# Local models have no per-token price and never reach it.
PIPELINE_MAX_COST_USD: float = float(os.getenv("PIPELINE_MAX_COST_USD", "2.0"))

# Shadow book: a hypothetical portfolio kept so that exits get exercised
# before there is a paper broker. Fixed dollars per position, filled at the
# next session's open.
SHADOW_POSITION_USD: float = float(os.getenv("SHADOW_POSITION_USD", "10000"))
SHADOW_MAX_POSITIONS: int = int(os.getenv("SHADOW_MAX_POSITIONS", "10"))
# An order with no price to fill at is dropped after this many calendar days,
# and a position with no usable price for as long is closed at its last good one.
SHADOW_PENDING_MAX_DAYS: int = int(os.getenv("SHADOW_PENDING_MAX_DAYS", "10"))


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
