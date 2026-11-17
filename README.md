# Financial Market Simulation — Multi-Agent Framework

An AI-powered multi-agent system that simulates how financial markets react to macroeconomic, monetary, and fiscal policy changes. It has a **ReAct-style agentic orchestrator** that dynamically decides its analysis strategy, specialist domain agents, structured adversarial debate, vector memory (ChromaDB), live market and macro data saved as point-in-time snapshots, and user preference steering.

The model provider is configurable. By default it runs open-source models on your own machine through Ollama; it can also use Claude or any service with an OpenAI-compatible API.

There are two ways to use it:

- **Research mode** (`main.py`, `app.py`): describe a policy scenario and get a report on how markets might react. Most of this document describes it.
- **Daily trading pipeline** (`python -m pipeline.daily`): a fixed pipeline that turns each day's market data into buy, sell, hold and trim signals for US stocks. It runs in **shadow mode**: it records signals and keeps a hypothetical book, and places no orders. See [Daily Trading Pipeline](#daily-trading-pipeline-shadow-mode).

Nothing this system produces is financial advice. Its outputs are model outputs, and there is no evidence yet that they beat a benchmark; `docs/UPGRADE_PLAN.md` describes how that is to be tested.

## Architecture

The orchestrator is a **ReAct agent loop** — instead of following a fixed pipeline, the orchestrator LLM reasons about each scenario and dynamically decides which tools to use, which specialists to consult, and when to synthesize.

```mermaid
graph TD
    START((User Scenario)) --> REASON

    REASON["Orchestrator LLM<br/><i>Reason about what this scenario needs</i>"]

    REASON -->|"need structure"| PARSE["parse_scenario<br/><i>Classify type, magnitude, surprise</i>"]
    REASON -->|"need data"| MARKET["fetch_market_data<br/><i>Markets, yields, sectors, macro series</i>"]
    REASON -->|"need history"| MEMORY["search_memory<br/><i>Similar past simulations (ChromaDB)</i>"]
    REASON -->|"need analysis"| CONSULT["consult_specialist<br/><i>1-7 domain agents, parallel</i>"]
    REASON -->|"need calibration"| DEBATE["run_debate<br/><i>Severe vs contained + Judge verdict</i>"]
    REASON -->|"need resolution"| DELIBERATE["run_deliberation<br/><i>Agents revise after seeing each other</i>"]
    REASON -->|"analysis complete"| REPORT["submit_report<br/><i>Final synthesis</i>"]

    PARSE --> REASON
    MARKET --> REASON
    MEMORY --> REASON
    CONSULT --> REASON
    DEBATE --> REASON
    DELIBERATE --> REASON
    REPORT --> END((Report))

    style START fill:#2d9,stroke:#333,color:#fff
    style END fill:#2d9,stroke:#333,color:#fff
    style REASON fill:#f90,stroke:#333,color:#fff
    style PARSE fill:#48f,stroke:#333,color:#fff
    style MARKET fill:#0a8,stroke:#333,color:#fff
    style MEMORY fill:#0a8,stroke:#333,color:#fff
    style CONSULT fill:#a3f,stroke:#333,color:#fff
    style DEBATE fill:#e55,stroke:#333,color:#fff
    style DELIBERATE fill:#48f,stroke:#333,color:#fff
    style REPORT fill:#2d9,stroke:#333,color:#fff
```

### How the Agent Loop Works

1. The scenario is parsed into structured fields (type, magnitude, surprise)
2. The orchestrator model **reasons** about what this specific scenario requires
3. It **chooses one action** — fetch data, consult agents, debate, etc. — returned as a validated structured object
4. The code runs the action and adds the result to the step history
5. The model **reasons again** — do I need more analysis, or am I ready to synthesize?
6. Loop continues until it chooses `submit_report` or hits the iteration cap (25)

The loop does not use any vendor's tool-calling format, so it behaves the same on every provider. The code also enforces a few rules the model cannot override: an agent is consulted once, one-shot actions do not repeat, and a report cannot be submitted before any specialist has been consulted.

### Adaptive Behavior

The orchestrator adapts its strategy based on scenario complexity:

| Scenario Type | Typical Strategy | Tool Calls |
|--------------|-----------------|------------|
| Simple/clear (e.g. "Fed raises 25bp") | Parse → 2-3 key agents → report | 3-4 |
| Moderate (e.g. "fiscal stimulus + tax reform") | Parse → market data → 4-5 agents in tiers → report | 5-7 |
| Complex/ambiguous (e.g. "trade war + currency devaluation + supply shock") | Parse → data → memory → all agents in tiers → deliberation → debate → report | 8-12 |

### Why Agentic Instead of a Fixed Pipeline?

- **Scenario-aware**: A simple rate change doesn't need 7 agents, debate, and deliberation. The orchestrator skips what's unnecessary.
- **Dynamic ordering**: The LLM decides which agents should inform which — not a hardcoded routing table.
- **User-steerable**: Preferences like "depth=deep" or "focus=equity_market" change the orchestrator's strategy, not just parameters.
- **Self-correcting**: If agent analyses conflict, the orchestrator can choose to run deliberation or debate — or neither if the conflict is minor.
- **Extensible**: Adding a new capability means adding a new tool definition — the orchestrator discovers when to use it.

## Orchestrator Actions

Scenario parsing always runs first and is not a choice. After that the orchestrator picks from six actions:

| Action | Purpose | When Used |
|------|---------|-----------|
| `fetch_market_data` | Capture a data snapshot: indices, yields, sectors, macro series, plus any tickers named | When current conditions matter |
| `search_memory` | Find similar past simulations in ChromaDB | When historical context exists |
| `consult_specialist` | Run 1+ domain agents (parallel if multiple) | Core analysis step |
| `run_debate` | Two-sided debate on how large the impact is | When impact is genuinely uncertain |
| `run_deliberation` | Agents review and revise each other's analyses | When agents materially disagree |
| `submit_report` | Produce final report — ends the loop | When analysis is sufficient |

## Agent Interaction Model

The system has **five levels** of inter-agent communication, all triggered dynamically:

1. **Tiered context passing**: The orchestrator can consult upstream agents first (central bank, fiscal) and pass their outputs as context to downstream agents (equities, forex). Unlike the old fixed pipeline, the orchestrator decides the tier structure per-scenario.

2. **Deliberation** (orchestrator-triggered): When the orchestrator notices agent analyses conflict, it can invoke deliberation — each agent sees all others' analyses and can revise or defend its position.

3. **Adversarial debate** (orchestrator-triggered): One side argues the impact will be severe, the other that it will be contained. They alternate for multiple rounds. A Judge produces a calibrated verdict on the size of the impact.

4. **Memory-informed analysis**: Past simulation outcomes are retrieved and injected into agent context, so analyses account for historical patterns.

5. **Market-data grounding**: A data snapshot grounds analyses in current conditions rather than training knowledge alone. Agents see it in its own section, separate from other agents' conclusions, and are told that every number they state must come from it or from the scenario.

## Agents

| Agent | Domain | Key Outputs |
|-------|--------|-------------|
| Central Bank | Monetary policy, rates, QE/QT | Rate path, liquidity conditions |
| Fiscal Policy | Gov spending, taxes, deficits | Demand impact, debt trajectory |
| Commodities & Inflation | Prices, CPI/PPI | Inflation path, commodity moves |
| Equity Market | Stock sectors, valuations | Sector rotation, P/E shifts |
| Fixed Income | Bonds, yield curve, spreads | Yield moves, curve shape |
| Forex | Exchange rates, capital flows | Currency direction, carry trade |
| Sentiment & Behavioral | Psychology, risk appetite | Risk-on/off, positioning |

## Setup

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd financial-market-sim

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Choose a model provider
cp .env.example .env
# Default is LLM_PROVIDER=ollama (local open-source models, no API key).
# For Claude set LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY.
```

### Running on local open-source models

```bash
# Install Ollama from https://ollama.com, then pull the default models
ollama pull qwen2.5-coder:7b
ollama pull llama3.2
```

Any Ollama model that supports structured output works; set `LLM_MODEL` and `LLM_MODEL_FAST` in `.env`. On a 16 GB machine expect models up to roughly 14B parameters and several minutes per simulation.

## Usage

### CLI Mode

```bash
# Activate virtual environment
source venv/bin/activate

# Interactive mode (REPL — type scenarios, get reports)
python main.py

# Single scenario
python main.py --scenario "The Fed cuts rates by 50bps unexpectedly"

# With preferences (depth, focus domains, skip debate)
python main.py --scenario "Fed raises rates" --preferences "depth=deep,focus=equity_market,focus=forex"

# Compare two scenarios side-by-side
python main.py --compare \
  --scenario-a "Fed raises rates 25bps" \
  --scenario-b "Fed raises rates 100bps"

# Override provider, model or economy
python main.py --scenario "ECB begins QT" --economy EU --provider anthropic --model claude-sonnet-5-5
```

Every run starts by printing what is actually working (model, memory, market data) and ends with a usage summary (calls, tokens, time, estimated cost).

### User Preferences

Steer the orchestrator's analysis strategy via preferences:

```bash
# CLI
python main.py -s "scenario" -p "depth=deep,focus=equity_market,focus=forex"

# Interactive mode
scenario> prefs depth=deep,focus=equity_market
scenario> The Fed raises rates by 50bps
```

| Preference | Values | Effect |
|-----------|--------|--------|
| `depth` | quick / standard / deep | Controls how many agents and rounds the orchestrator uses |
| `focus` | Any agent domain name | Orchestrator prioritizes these domains |
| `horizon` | immediate / short / medium | Emphasizes a specific time horizon |
| `no_debate` | (flag) | Orchestrator skips the debate step |

### Web UI (Streamlit)

```bash
streamlit run app.py
```

### Python API

```python
from orchestrator import Orchestrator

orch = Orchestrator()

# Basic simulation
report = orch.run("What happens if the Fed raises rates by 100bps unexpectedly?")
print(report.to_markdown())

# With preferences
report = orch.run(
    "Fed raises rates 50bps",
    preferences={"depth": "deep", "focus_domains": ["fixed_income", "forex"]},
)

# Compare two scenarios
comparison = orch.compare(
    "Fed raises rates 25bps",
    "Fed raises rates 100bps",
)
print(comparison)
```

## Key Features

### ReAct Agent Loop
Each turn the orchestrator model returns a structured `NextAction` (which action, which agents, why). The code executes it and feeds the result back. This replaces the old fixed LangGraph pipeline, which was removed and remains in the git history as `orchestrator_legacy.py`.

### Switchable Model Provider
All model calls go through one interface in `utils/llm_client.py` with three providers: `ollama`, `openai_compat` and `anthropic`. Agents and the orchestrator ask for a Pydantic object and never touch a vendor SDK. A main tier and a fast tier can use different providers and models.

### Vector Memory (ChromaDB)
Every completed simulation is stored in a ChromaDB vector store. On the next run, the orchestrator can search for similar past scenarios and include their outcomes in agent context. This enables **cross-run learning**.

### Data Layer and Point-in-Time Snapshots
The `data/` package collects everything the system knows at one moment into a validated `Snapshot` and saves it to disk:

- **Prices** for any stock or ETF: daily bars, returns, 52-week range, moving averages, RSI, MACD, Bollinger bands, ATR, dividends and splits.
- **Fundamentals, next earnings date and recent headlines** per ticker.
- **Market-wide series**: equity indices, VIX, dollar, gold, oil, Treasury yields, 11 sector ETFs.
- **Macro series** from FRED: policy rate, CPI and core CPI, unemployment, payrolls, yields, credit spread, oil.

Sources are Yahoo Finance (through `yfinance`) and FRED's public CSV download. Both are free and need no key, and each sits behind a small interface so it can be replaced.

Three rules the data layer enforces:

- **Only completed sessions.** While the market is open, today's unfinished bar is left out. Trading days, holidays and early closes come from an NYSE calendar.
- **Refuse bad data.** A ticker with stale prices, too little history, a broken bar, no volume or an unexplained jump is marked not usable, with the reason.
- **Keep what was seen.** Every fetch is saved under `data_store/snapshots/<session date>/`, so a past analysis can be examined against exactly the data it used.

Capture a snapshot from the command line:

```bash
python -m data.snapshot --tickers AAPL,MSFT
```

The command exits non-zero when any requested ticker is not usable.

The same data is available to outside MCP clients through `mcp_tools/yfinance_server.py`. The simulator itself calls the data layer directly.

### Adversarial Debate
One side argues the impact will be severe, the other that it will be contained, and a Judge gives a calibrated verdict. The orchestrator decides whether a debate adds value for each scenario.

The sides are named by impact size on purpose. They used to be called Bull and Bear, which mean market direction, and that made the report's scenario matrix come out inverted. In the matrix, "Bull case" means better for risk assets than the base case and "Bear case" means worse.

### Independence Checks
The value of several agents comes from their views being independent, and a weak model will paste other agents' sentences into its own analysis. Two guards in code:

- After deliberation, any revised field that mostly repeats other agents is replaced by the agent's own earlier text.
- In the final report, pairs of agents that share a long passage word for word are listed under "Quality Warnings".

### Figure Check
Agents are told that every number they state must come from the data snapshot or the scenario. A weak model ignores this and invents index levels and yields, so the code checks: every figure in each agent's analysis and in the report's summary sections is compared with the numbers in the snapshot, the scenario, and the agent's own instructions. Figures found in none of them are listed under "Quality Warnings", by agent.

The check is lenient on purpose. A figure passes if any number shown rounds to it, so "about 5%" passes when the data says 5.15%. Durations, dates and index names are not treated as figures. A figure the model derived by arithmetic is listed too, because the check cannot tell arithmetic from invention; the warning says "not found", not "wrong". It only reports and never rewrites an agent's text.

### Structured Output
Every structured call is validated against a Pydantic model. Ollama and OpenAI-compatible services get a JSON schema constraint; Claude uses the SDK's structured outputs. A reply that fails validation is retried with the error shown to the model. For Claude, system prompts are cached via `cache_control`.

## Daily Trading Pipeline (shadow mode)

```bash
python -m pipeline.daily              # run for the last completed US session
python -m pipeline.daily --status     # past runs, and progress towards the stage's exit test
python -m pipeline.daily --arm ollama:qwen2.5-coder:7b --arm anthropic:claude-haiku-5-5   # compare two models
```

Other options: `--force` (run a session again), `--refresh-macro` (form a new macro view now), `--universe FILE`, `--quiet`.

The stages run in an order fixed in code. Models work inside a stage and never choose the next one.

| Stage | Who | What happens |
|---|---|---|
| Data | code | Screen the universe on price, liquidity and trend. Take one snapshot of the candidates and of everything the book holds. |
| Settle | code | Fill earlier proposals in the shadow book at the open of the session after they were made. |
| Macro | 7 models, then code | Each macro agent assesses the current state of its domain. Code combines the stances into the desk's macro view: the stance with the most confidence behind it, and sector tilts only where more than one agent's worth of confidence agrees. Reused for a week. |
| Views | 1 model | The ticker analyst writes one view per candidate and per holding: direction, confidence, horizon, invalidation, cited data. |
| Debate | 3 models | Bull, bear and judge argue the strongest new candidates and any holding whose view has turned. |
| Proposals | 1 model | The portfolio manager proposes buy, sell, hold or trim. |
| Checks | code | Each proposal is accepted or rejected by rule. Every holding gets a decision; a missing one is "hold". |

**Models propose, code decides.** A model's output is a draft. Code checks it and keeps it, or refuses it and records why. Nothing is repaired silently: a confidence outside 0 to 1 is refused, not clamped. The rules for proposals:

- The ticker must be one of today's candidates or holdings. Only the first proposal for a ticker counts.
- A buy needs a checked analyst view of that ticker that is bullish, no ruling against it from the judge, a current macro view, and room in the book.
- Sell, trim and hold apply only to positions the book holds, and a position with an exit already waiting gets no second order.
- Buys are taken in order of confidence until the book is full. A position being sold today still counts as held.

These rules are also what limits the damage of a persuaded model. Headlines are outside text, and a model can be talked into things; code still only lets it buy a screened candidate that the analyst and the judge did not reject, up to the position limit, or exit what the book already holds.

**When to run it.** After the close, once closing prices are published, and before the next session opens. Yahoo Finance can take hours after the close to publish them; until then the run fails with a message saying so and can simply be repeated. Once the next session has opened the run is refused: its signals would rest on that day's news and prices, and would be filled at an open that has already passed. The same check is made again after the data is collected and before orders are queued, so a run that is overtaken by the open queues nothing. With a local 7B model a run takes five to eight minutes.

A session that already has signals is skipped, so the command is safe to schedule and to repeat. A run that started less than six hours ago and has not finished is taken to be still going: the command reports `blocked` and exits non-zero, and `--force` overrides it after a crash.

**When a run does not count.** A run fails, and proposes nothing, when the price source is down, when fewer than half the universe has usable prices for the session, when the next session is already trading, when the model is unreachable, or when the cost limit is reached. A run that finished but lost model calls, had outputs refused, had data missing, or could not analyse something the book holds is marked `degraded`.

**The shadow book.** Shadow mode has no broker, so each accepted proposal is treated as an order for a fixed dollar amount that fills at the next session's open. This gives the portfolio manager real positions to hold, trim and sell. It has no cash balance, dividends or slippage. Splits are handled: the price source restates all earlier prices after a split, so positions are moved onto each new snapshot's share scale before anything is filled or valued. The move is only made when the listed split and the restated prices agree; when they do not, the position is left unvalued, its orders wait, and the run is marked degraded. A position that runs have found without a usable, consistent price for more than `SHADOW_PENDING_MAX_DAYS` days, such as a delisted one, is closed by the book at the last close it could trust. Days on which the pipeline did not run are not counted.

**Checked citations.** The analyst names the data points it relied on (`rsi14`, `sma50`, `trailing_pe`, ...) with their values. Code compares each with the snapshot and records mismatches on the view, along with any figure in the text that appears nowhere in the data shown. The portfolio manager and the judge see these warnings.

**What is recorded.** Each run writes `signals.json` and `signals.md` under `data_store/runs/<session>/<run id>/`, and everything goes into the SQLite journal at `data_store/journal.sqlite3`: runs with their cost, every event (who said what to whom, in order, in the model's own words), macro views, and the shadow book with its trades. Orders, the fills a run reported and the run's final status are saved in one transaction. The journal carries a format number, and a file written under another format is refused, not misread. It uses SQLite's write-ahead log, which adds `-wal` and `-shm` files beside it and needs a local disk: do not point `JOURNAL_PATH` at a network or synced folder.

**Comparing models.** Each `--arm` is a model configuration. All arms read the same data snapshot; each keeps its own signals and its own shadow book.

Each stage of the larger plan has an exit test. For this one it is five trading sessions in a row without error, with at least one sell or trim accepted along the way; `--status` shows where that stands.

## Configuration

Set these in `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `ollama` | `ollama`, `openai_compat` or `anthropic` |
| `LLM_MODEL` | provider default | Main model for orchestrator and agents |
| `LLM_MODEL_FAST` | provider default | Fast model for parsing |
| `LLM_PROVIDER_FAST` | same as `LLM_PROVIDER` | Provider for the fast tier |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where Ollama is running |
| `OLLAMA_NUM_CTX` | `8192` | Context window; prompts that would not fit are refused |
| `OPENAI_COMPAT_BASE_URL` | — | Base URL of an OpenAI-compatible service |
| `OPENAI_COMPAT_API_KEY` | — | Key for that service |
| `ANTHROPIC_API_KEY` | — | Required only for `LLM_PROVIDER=anthropic` |
| `BASE_ECONOMY` | `US` | Default economy for simulations |
| `MAX_ORCHESTRATOR_ITERATIONS` | `25` | Max tool-call iterations per simulation |
| `MAX_DEBATE_ROUNDS` | `2` | Maximum number of debate rounds |
| `MEMORY_ENABLED` | `true` | Enable cross-run ChromaDB memory |
| `MEMORY_DIR` | `.memory` | Directory for ChromaDB persistence |
| `MEMORY_RETRIEVE_K` | `3` | Number of similar past scenarios to retrieve |
| `MARKET_DATA_ENABLED` | `true` | Let the orchestrator fetch market and macro data |
| `SNAPSHOT_DIR` | `data_store/snapshots` | Where data snapshots are saved |
| `UNIVERSE_FILE` | built-in list | Tickers the pipeline screens, one per line |
| `PIPELINE_MAX_CANDIDATES` | `10` | Candidates kept by the screen each day |
| `PIPELINE_MIN_PRICE` | `5` | Screen: minimum share price |
| `PIPELINE_MIN_DOLLAR_VOLUME` | `20000000` | Screen: minimum 20-day average dollar volume |
| `PIPELINE_MACRO_MAX_AGE_DAYS` | `7` | The macro view is reused until it is this old |
| `PIPELINE_DEBATE_TOP` | `3` | New candidates debated each day |
| `PIPELINE_DEBATE_ROUNDS` | `1` | Rounds per debate |
| `PIPELINE_MAX_COST_USD` | `2.0` | A run stops once its model calls have cost this much |
| `SHADOW_POSITION_USD` | `10000` | Dollars per shadow position |
| `SHADOW_MAX_POSITIONS` | `10` | Most positions the shadow book may hold |
| `SHADOW_PENDING_MAX_DAYS` | `10` | An order with no usable price is dropped after this many days, and a position with none is closed |
| `JOURNAL_PATH` | `data_store/journal.sqlite3` | The SQLite journal |
| `RUNS_DIR` | `data_store/runs` | Where signal tables are written |

The pipeline's numbers are placeholders, not recommendations.

## Example Scenarios

- *"The Fed raises interest rates by 100bps unexpectedly while inflation is at 6%"*
- *"Congress passes a $2 trillion infrastructure stimulus funded by new corporate taxes"*
- *"China devalues the yuan by 10% in response to trade tariffs"*
- *"The ECB begins quantitative tightening while the US cuts rates"*
- *"Oil prices spike to $150/barrel due to Middle East supply disruption"*

## Project Structure

```
financial-market-sim/
├── config.py              # Provider, model and feature configuration
├── orchestrator.py        # ReAct agentic orchestrator (structured action loop)
├── docs/UPGRADE_PLAN.md   # Plan for evolving this into a trading system
├── tests/                 # pytest suite (no real model needed)
├── main.py                # CLI entry point (Rich terminal UI, preferences)
├── app.py                 # Streamlit web UI
├── requirements.txt       # Python dependencies
├── .env                   # API key (not committed)
├── .env.example           # Template for .env
├── pipeline/
│   ├── daily.py           # The fixed daily pipeline (shadow mode) and its command line
│   ├── universe.py        # Ticker universe and the rule-based candidate screen
│   ├── macro.py           # Combines the macro agents' assessments by rule
│   ├── checks.py          # Rules between model output and what is kept
│   ├── shadow.py          # Shadow book: hypothetical fills at the next open
│   └── report.py          # Briefings and the Markdown signal table
├── journal/
│   └── store.py           # SQLite journal: runs, events, macro views, shadow book
├── agents/
│   ├── ticker/            # Ticker analyst, bull, bear, judge, portfolio manager
│   ├── base.py            # BaseAgent: scenario analysis, deliberation, current-state assessment
│   ├── central_bank.py    # Monetary policy specialist
│   ├── fiscal_policy.py   # Government spending specialist
│   ├── commodities.py     # Commodities & inflation specialist
│   ├── equity_market.py   # Stock market specialist
│   ├── fixed_income.py    # Bond market specialist
│   ├── forex.py           # Currency specialist
│   └── sentiment.py       # Market psychology specialist
├── memory/
│   └── store.py           # ChromaDB vector store for cross-run learning
├── data/
│   ├── market_calendar.py # NYSE trading days, holidays, early closes
│   ├── providers.py       # Yahoo Finance and FRED behind small interfaces
│   ├── indicators.py      # Moving averages, RSI, MACD, Bollinger, ATR
│   ├── quality.py         # Checks that mark a ticker's data usable or not
│   ├── snapshot.py        # Build, save and load snapshots; command line
│   ├── render.py          # Snapshot as the text shown to models
│   └── models.py          # Snapshot data models
├── mcp_tools/
│   └── yfinance_server.py # MCP server exposing the data layer to outside clients
├── models/
│   ├── scenario.py        # Pydantic model for parsed scenarios
│   ├── report.py          # Pydantic models for agent output and reports
│   └── signals.py         # Pydantic models for the pipeline's signals
└── utils/
    ├── llm_client.py      # One LLM interface over Ollama, OpenAI-compatible and Claude
    ├── text_overlap.py    # Detects agents copying each other's text
    └── grounding.py       # Finds stated figures that match nothing the model was shown
```

## Tests

```bash
pytest                               # unit tests, no model needed
RUN_LIVE=1 pytest -m live            # structured output against local Ollama
RUN_LIVE_ANTHROPIC=1 pytest -m live  # the same against Claude (costs a fraction of a cent)
```

## License

MIT
