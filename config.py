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

# --- Daily trading pipeline (paper trading: simulated orders only) ---
# Every number here is a placeholder until the user sets it.
JOURNAL_PATH: str = os.getenv("JOURNAL_PATH", "").strip() or str(PROJECT_ROOT / "data_store" / "journal.sqlite3")
RUNS_DIR: str = os.getenv("RUNS_DIR", "").strip() or str(PROJECT_ROOT / "data_store" / "runs")
# One symbol per line. Empty means the built-in list in pipeline/universe.py.
UNIVERSE_FILE: str = os.getenv("UNIVERSE_FILE", "").strip()

# Candidate screen: plain rules, no model involved.
PIPELINE_MAX_CANDIDATES: int = int(os.getenv("PIPELINE_MAX_CANDIDATES", "10"))
PIPELINE_MIN_PRICE: float = float(os.getenv("PIPELINE_MIN_PRICE", "5"))
PIPELINE_MIN_DOLLAR_VOLUME: float = float(os.getenv("PIPELINE_MIN_DOLLAR_VOLUME", "20000000"))

# The macro team's view is reused until it is this many days old. 1: it is formed anew every
# session, and only a second run of the same session reuses it. It was 7 until 2026-11-22: on
# 20 November the view in use was three sessions old, "neutral", after the S&P 500 had fallen 5.9%.
PIPELINE_MACRO_MAX_AGE_DAYS: int = int(os.getenv("PIPELINE_MACRO_MAX_AGE_DAYS", "1"))
# How many of the strongest new candidates get a bull/bear debate, and how many rounds each.
PIPELINE_DEBATE_TOP: int = int(os.getenv("PIPELINE_DEBATE_TOP", "3"))
PIPELINE_DEBATE_ROUNDS: int = int(os.getenv("PIPELINE_DEBATE_ROUNDS", "1"))
# A run stops, and proposes nothing, once its model calls have cost this much.
# Local models have no per-token price and never reach it.
PIPELINE_MAX_COST_USD: float = float(os.getenv("PIPELINE_MAX_COST_USD", "2.0"))

# --- Paper broker ---
# A simulated account matched against daily bars. No real money and no real orders.
PAPER_INITIAL_CASH: float = float(os.getenv("PAPER_INITIAL_CASH", "100000"))
# Every fill is worse than the quoted price by this many basis points.
PAPER_SLIPPAGE_BPS: float = float(os.getenv("PAPER_SLIPPAGE_BPS", "5"))
# An order with no usable price is cancelled after this many calendar days, and
# a position that syncs have found without one for as long is closed at its
# last good close.
PAPER_STALE_DAYS: int = int(os.getenv("PAPER_STALE_DAYS", "10"))

# --- Position sizing ---
# Share of equity put at risk on one new position when confidence is 1.0.
# Lower confidence scales it down in proportion.
SIZING_RISK_PCT: float = float(os.getenv("SIZING_RISK_PCT", "1.0"))
# The protective stop sits this many ATRs below the last close, and never
# closer than this percent of the price: a stop a few cents away fires on noise.
SIZING_STOP_ATR_MULTIPLE: float = float(os.getenv("SIZING_STOP_ATR_MULTIPLE", "2.0"))
SIZING_MIN_STOP_PCT: float = float(os.getenv("SIZING_MIN_STOP_PCT", "1.0"))
# The analyst may propose a stop of its own for a new position. It is used only
# when it sits between this many ATRs below the last close; outside that range
# it is refused and the stop above is used instead.
SIZING_STOP_MIN_ATR_MULTIPLE: float = float(os.getenv("SIZING_STOP_MIN_ATR_MULTIPLE", "1.0"))
SIZING_STOP_MAX_ATR_MULTIPLE: float = float(os.getenv("SIZING_STOP_MAX_ATR_MULTIPLE", "4.0"))

# --- Risk gate ---
# Hard limits enforced in code on every order. All of them are conservative
# placeholders for the user to set. Percentages are of account equity.
RISK_MAX_POSITION_PCT: float = float(os.getenv("RISK_MAX_POSITION_PCT", "10"))
RISK_MAX_SECTOR_PCT: float = float(os.getenv("RISK_MAX_SECTOR_PCT", "30"))
RISK_MAX_POSITIONS: int = int(os.getenv("RISK_MAX_POSITIONS", "10"))
RISK_MIN_CASH_PCT: float = float(os.getenv("RISK_MIN_CASH_PCT", "10"))
# Largest loss one new position may take if its stop is hit.
RISK_MAX_TRADE_RISK_PCT: float = float(os.getenv("RISK_MAX_TRADE_RISK_PCT", "1.0"))
RISK_MAX_NEW_POSITIONS_PER_DAY: int = int(os.getenv("RISK_MAX_NEW_POSITIONS_PER_DAY", "3"))
# Value of all of a day's orders, buys and sells together.
RISK_MAX_DAILY_TURNOVER_PCT: float = float(os.getenv("RISK_MAX_DAILY_TURNOVER_PCT", "30"))
# Circuit breakers: once tripped, no new positions are opened. Exits still go through.
RISK_DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("RISK_DAILY_LOSS_LIMIT_PCT", "3"))
RISK_MAX_DRAWDOWN_PCT: float = float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "10"))
RISK_MIN_PRICE: float = float(os.getenv("RISK_MIN_PRICE", "5"))
RISK_MIN_DOLLAR_VOLUME: float = float(os.getenv("RISK_MIN_DOLLAR_VOLUME", "20000000"))
# A limit price may not be further than this from the last close.
RISK_MAX_LIMIT_DEVIATION_PCT: float = float(os.getenv("RISK_MAX_LIMIT_DEVIATION_PCT", "5"))
# Kill switch: while this file exists, or TRADING_HALT is set to 1, no order of any kind is placed.
KILL_SWITCH_FILE: str = os.getenv("KILL_SWITCH_FILE", "").strip() or str(PROJECT_ROOT / "data_store" / "HALT")

# --- Evaluation ---
# What the views and trades turned out to be worth, measured after the fact.
# Run with `python -m evaluation`. It reads the journal and later prices, and
# changes no decision the pipeline has made or will make.
EVALUATION_DIR: str = os.getenv("EVALUATION_DIR", "").strip() or str(PROJECT_ROOT / "data_store" / "evaluation")
# What every view and the account are measured against.
EVALUATION_BENCHMARK: str = os.getenv("EVALUATION_BENCHMARK", "").strip().upper() or "SPY"
# How many times the account's buys are replaced by random picks from the same day's candidates.
EVALUATION_SIMULATIONS: int = int(os.getenv("EVALUATION_SIMULATIONS", "1000"))
# Fixed, so that two evaluations of the same records draw the same random picks.
EVALUATION_SEED: int = int(os.getenv("EVALUATION_SEED", "20261117"))
# Lessons a model writes about its own past views, once their real outcome is
# known: at most this many per evaluation, and only when asked for (--lessons).
EVALUATION_MAX_LESSONS: int = int(os.getenv("EVALUATION_MAX_LESSONS", "5"))
# Whether the ticker analyst is shown those lessons. Off by default: turning it
# on changes what the analyst reads, and so what the pipeline decides.
PIPELINE_USE_LESSONS: bool = os.getenv("PIPELINE_USE_LESSONS", "false").strip().lower() == "true"
PIPELINE_LESSONS_PER_TICKER: int = int(os.getenv("PIPELINE_LESSONS_PER_TICKER", "3"))

# --- The desk: meetings through the trading day (paper trading: simulated orders only) ---
# A second group beside the daily pipeline, with its own paper account. It is
# driven by a short job that runs every few minutes: python -m pipeline.desk.
# Each round fetches headlines and prices for the book into the ledger, and
# holds a meeting when one is due. The daily pipeline is not changed by it.
LEDGER_PATH: str = os.getenv("LEDGER_PATH", "").strip() or str(PROJECT_ROOT / "data_store" / "ledger.sqlite3")
# Check-ins, as New York times. One before the open and up to two during the
# session; a time at or after the day's close is skipped on that day.
DESK_PREMARKET: str = os.getenv("DESK_PREMARKET", "").strip() or "08:30"
DESK_MIDDAY: str = os.getenv("DESK_MIDDAY", "").strip() or "12:00"
DESK_AFTERNOON: str = os.getenv("DESK_AFTERNOON", "").strip() or "15:00"
# (How often a round runs is the schedule's business: DESK_WATCH_MINUTES, read by scripts/schedule.sh.)
# The close meeting is first tried this long after the close: the source
# publishes final prices late. A failed try is repeated no sooner than
# DESK_RETRY_MINUTES later, and at most DESK_CLOSE_TRIES times.
DESK_CLOSE_DELAY_MINUTES: int = int(os.getenv("DESK_CLOSE_DELAY_MINUTES", "120"))
DESK_RETRY_MINUTES: int = int(os.getenv("DESK_RETRY_MINUTES", "60"))
DESK_CLOSE_TRIES: int = int(os.getenv("DESK_CLOSE_TRIES", "5"))
DESK_CHECKIN_TRIES: int = int(os.getenv("DESK_CHECKIN_TRIES", "2"))
# Headlines are shown for this many days after they appear, and at most this many per ticker.
DESK_HEADLINE_DAYS: int = int(os.getenv("DESK_HEADLINE_DAYS", "7"))
DESK_HEADLINES_SHOWN: int = int(os.getenv("DESK_HEADLINES_SHOWN", "8"))
# A price move counts as worth a look when it is this many times the usual
# daily range (ATR 14), or this many percent where the range is unknown.
DESK_MOVE_ATR: float = float(os.getenv("DESK_MOVE_ATR", "1.0"))
DESK_MOVE_PCT: float = float(os.getenv("DESK_MOVE_PCT", "3.0"))

# --- The forecast engine (plain code, no model is called; it places and changes no order) ---
# At the desk's close meeting the quant engineer's seat says what the price of each name under
# discussion may do over the next 1, 5, 10 and 20 sessions, as a distribution: a Markov chain for
# the market's state, and for each name a diffusion with jumps around it. Every forecast is kept
# and set against what happened once its horizon has ended.
FORECAST_ENABLED: bool = os.getenv("FORECAST_ENABLED", "true").strip().lower() == "true"
# Where each week's parameters are kept, one file per version.
FORECAST_DIR: str = os.getenv("FORECAST_DIR", "").strip() or str(PROJECT_ROOT / "data_store" / "forecast")
# Paths simulated for every name at every meeting.
FORECAST_PATHS: int = int(os.getenv("FORECAST_PATHS", "10000"))
# States of the market (2: calm and turbulent; 3 adds crisis).
FORECAST_STATES: int = int(os.getenv("FORECAST_STATES", "2"))
# Years of daily bars the estimate reads: the index for the states, each name for everything else.
FORECAST_MARKET_YEARS: int = int(os.getenv("FORECAST_MARKET_YEARS", "20"))
FORECAST_HISTORY_YEARS: int = int(os.getenv("FORECAST_HISTORY_YEARS", "5"))
# Volatility is an exponentially weighted average of squared daily moves; this is the weight on yesterday's.
FORECAST_EWMA_LAMBDA: float = float(os.getenv("FORECAST_EWMA_LAMBDA", "0.94"))
# A day's own move beyond this many of its expected standard deviations counts as a jump, not as ordinary noise.
FORECAST_JUMP_SIGMAS: float = float(os.getenv("FORECAST_JUMP_SIGMAS", "4.0"))
# With fewer past report days than this, a name's earnings moves are drawn from its whole sector's.
FORECAST_MIN_EARNINGS: int = int(os.getenv("FORECAST_MIN_EARNINGS", "8"))
# A name's own average daily return counts for days / (days + this) of itself: with five years
# of bars, a fifth. An average from a few hundred days is mostly noise.
FORECAST_DRIFT_SHRINK_DAYS: int = int(os.getenv("FORECAST_DRIFT_SHRINK_DAYS", "5040"))
# No seat names a price target yet. Until one does, "target before stop" is asked of a notional
# target this many times as far above the close as the stop is below it.
FORECAST_TARGET_R: float = float(os.getenv("FORECAST_TARGET_R", "2.0"))
# How a record earns a say (the only way a model's words become a number in the simulation):
# nothing below this many scored cases; a hit rate has to clear a coin toss by this many
# standard errors; and an analyst's lean may shift a name's drift by at most this share of
# its volatility over the view's horizon.
FORECAST_TRACK_MIN: int = int(os.getenv("FORECAST_TRACK_MIN", "200"))
FORECAST_TRACK_Z: float = float(os.getenv("FORECAST_TRACK_Z", "1.64"))
FORECAST_TILT_CAP: float = float(os.getenv("FORECAST_TILT_CAP", "0.25"))

# --- Working back from how a trade ends (plain code; it places and changes no order) ---
# For each name with a stop the quant engineer's seat takes three endings within the trade's
# horizon (the target comes first, the stop comes first, neither) and says what the simulated
# paths that end each way went through. The seat is part of the forecast engine: with
# FORECAST_ENABLED off it says nothing.
# An ending on fewer simulated paths than this is too rare to say what led to it.
BACKWARD_MIN_PATHS: int = int(os.getenv("BACKWARD_MIN_PATHS", "100"))
# "The index falls (or rises) this many percent from today's close on the way" is one of the events looked for.
BACKWARD_MARKET_MOVE_PCT: float = float(os.getenv("BACKWARD_MARKET_MOVE_PCT", "5.0"))
# An earnings report "taken badly" (or well): the name opens at least this many of that day's expected moves away.
BACKWARD_REPORT_MOVES: float = float(os.getenv("BACKWARD_REPORT_MOVES", "1.0"))

# --- The scenario analyst (a model's seat at the desk's close meeting; its words change no order) ---
# After the forecast it is asked, name by name, what could happen that the simulation does not
# already contain. What it proposes is judgement, with probabilities nobody measured: the paths
# are read backwards a second time with its events put in (the rehearsal), that reading is marked
# as resting on judgement, and each event is scored once its days have passed. One model call
# per name it is asked about. Off, the seat is silent and there is no rehearsal.
SCENARIO_ENABLED: bool = os.getenv("SCENARIO_ENABLED", "true").strip().lower() == "true"
# Events kept for one name at one meeting.
SCENARIO_MAX_EVENTS: int = int(os.getenv("SCENARIO_MAX_EVENTS", "5"))
# The smallest one-day move, in percent, that counts as an event. Anything smaller is an ordinary day.
SCENARIO_MIN_MOVE_PCT: float = float(os.getenv("SCENARIO_MIN_MOVE_PCT", "1.0"))
# Names it is asked about at one meeting: holdings first, then the names the analyst is bullish on.
SCENARIO_MAX_NAMES: int = int(os.getenv("SCENARIO_MAX_NAMES", "8"))

# --- Dashboard ---
# A read-only web page over the journal: python -m dashboard. By default it
# listens on this machine only. It has no login, so do not point it at a
# network address unless everyone on that network may read the account.
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "").strip() or "127.0.0.1"
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8765"))


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
