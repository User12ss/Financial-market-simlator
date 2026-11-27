# Financial Market Simulation — Multi-Agent Framework

An AI-powered multi-agent system that simulates how financial markets react to macroeconomic, monetary, and fiscal policy changes. It has a **ReAct-style agentic orchestrator** that dynamically decides its analysis strategy, specialist domain agents, structured adversarial debate, vector memory (ChromaDB), live market and macro data saved as point-in-time snapshots, and user preference steering.

The model provider is configurable. By default it runs open-source models on your own machine through Ollama; it can also use Claude or any service with an OpenAI-compatible API.

There are two ways to use it:

- **Research mode** (`main.py`, `app.py`): describe a policy scenario and get a report on how markets might react. Most of this document describes it.
- **Daily trading pipeline** (`python -m pipeline.daily`): a fixed pipeline that turns each day's market data into buy, sell, hold and trim signals for US stocks, sizes them, puts them through a risk gate and places the result with a **paper broker**. Every order is simulated: there is no real broker and no real money. See [Daily Trading Pipeline](#daily-trading-pipeline-paper-trading).

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

## Daily Trading Pipeline (paper trading)

```bash
python -m pipeline.daily              # run for the last completed US session
python -m pipeline.daily --status     # past runs, and progress towards the stage's exit test
python -m pipeline.daily --arm ollama:qwen2.5-coder:7b --arm anthropic:claude-haiku-5-5   # compare two models
```

Other options: `--force` (start a session over), `--refresh-macro` (form a new macro view now), `--universe FILE`, `--quiet`.

The stages run in an order fixed in code. Models work inside a stage and never choose the next one.

| Stage | Who | What happens |
|---|---|---|
| Data | code | Screen the universe on price, liquidity and trend. Take one snapshot of the candidates and of everything the account holds or has an order on. |
| Settle | code | Bring the paper account up to the session: dividends, splits, stops, fills at the open, equity at the close. |
| Macro | 7 models, then code | Each macro agent assesses the current state of its domain. Code combines the stances into the desk's macro view: the stance with the most confidence behind it, and sector tilts only where more than one agent's worth of confidence agrees. Formed anew every session (`PIPELINE_MACRO_MAX_AGE_DAYS`); a second run of the same session reuses it. |
| Views | 1 model | The ticker analyst writes one view per candidate and per holding: direction, confidence, horizon, invalidation, cited data and, for a candidate it is bullish on, the price it would put the stop at. |
| Debate | 3 models | Bull, bear and judge argue the strongest new candidates and any holding whose view has turned. |
| Proposals | 1 model | The portfolio manager proposes buy, sell, hold or trim. |
| Checks | code | Each proposal is accepted or rejected by rule. Every holding gets a decision; a missing one is "hold", or "sell" where the analyst and the judge have both said exit (below). |
| Risk | code | Each accepted proposal is sized, then the risk gate approves it, reduces it or rejects it, with the reason. |
| Orders | code | What passed is placed with the paper broker, to fill at the next session's open. |

**Models propose, code decides.** A model's output is a draft. Code checks it and keeps it, or refuses it and records why. Nothing is repaired silently: a confidence outside 0 to 1 is refused, not clamped. The rules for proposals:

- The ticker must be one of today's candidates or holdings. Only the first proposal for a ticker counts.
- A buy needs a checked analyst view of that ticker that is bullish, no ruling against it from the judge, a current macro view, and room in the book.
- Sell, trim and hold apply only to positions the account holds, and a position with an exit already waiting gets no second order.
- A holding the manager gives no accepted decision for is held. One exception: where the manager answered and its answer does not name the holding at all, the analyst's view of it is bearish and the judge ruled against owning it, the holding is sold, and the record says "sold by default". Two voices argued for the exit and nobody for keeping it. A manager that named the holding, even in a proposal that was turned back, has spoken, and one whose call failed has decided nothing: both leave it held. The analyst's view of a ticker can turn from one day to the next, so this rule sells more often than a manager would who had been asked again.

These rules are also what limits the damage of a persuaded model. Headlines are outside text, and a model can be talked into things; code still only lets it buy a screened candidate that the analyst and the judge did not reject, with a stop inside a fixed range, the same money at risk wherever in that range the stop is, and limits it has no say in, or exit what the account already holds.

**Sizing.** A model says what and how sure; code says how much. A buy risks a fixed share of equity (`SIZING_RISK_PCT`, scaled by the lower of the manager's and the analyst's confidence) between the last close and a protective stop below it. The share count is that money divided by the distance to the stop as it will be placed, so a wider stop means fewer shares and the loss at the stop is never more than that money. The risk gate can then only cut the count further: with the placeholder limits, the single-position limit binds whenever the stop is closer than about a tenth of the price, and then a closer stop means less money at risk, not more shares. A sell exits the whole position, a trim half.

**Where the stop goes.** The analyst may name a stop for a candidate it is bullish on, with the level in the data it sits under. Code uses it only when it is between `SIZING_STOP_MIN_ATR_MULTIPLE` and `SIZING_STOP_MAX_ATR_MULTIPLE` ATRs below the last close, and never closer than `SIZING_MIN_STOP_PCT` of the price. A stop outside that range is refused, not moved to the nearest allowed price, and the view says so; the rest of the view stands. (A stop that is not a number at all fails the reply as a whole, like any other malformed field.) Figures in the reason are traced to the data like those in the thesis. A closer stop is hit more easily, on the entry day included, and a gap at the open costs more against it. With no usable stop from the analyst, the stop is `SIZING_STOP_ATR_MULTIPLE` ATRs below the close. The report shows, for every order, which of the two set its stop. A position already held keeps the stop it was opened with.

**The risk gate.** Every sized order is approved, reduced or rejected. All limits are settings, enforced in code, and every cut is recorded with the limit and the number that broke it:

- Long only. No position, no sell; a sell is cut to what is held.
- Whitelist, minimum price, minimum dollar volume, usable data.
- No buy without a protective stop below the price.
- Largest loss per trade if the stop is hit, largest single position, largest sector, minimum cash, most positions, most new positions in a day, most turnover in a day. A sector fund (XLK, XLP and the other nine) is counted under the sector of the stocks it holds, though the data gives a fund no sector: its sector's limit cannot be passed by buying the fund on top of the stocks. A fund of the whole market (SPY, QQQ, IWM) has no sector; tickers without one share one limit among them.
- Circuit breakers: after a day's loss or a drawdown beyond the limit, no new buying. Exits still go through.
- Kill switch: while the file `data_store/HALT` exists, or `TRADING_HALT=1` is set, no order of any kind is placed. The analysis still runs and is recorded.

**The paper broker.** A simulated account matched against daily bars. Per session, in this order: dividends are paid; stops on existing positions trigger (at the open if the session gaps below the stop, otherwise at the stop); limit orders fill at the limit or better; market orders fill at the open, sells before buys; and a position opened that day is then checked against its own stop. Orders fill whole or not at all, and market and stop fills are made worse by `PAPER_SLIPPAGE_BPS`. A buy carries its protective stop, which becomes a standing order when the buy fills and follows the position through trims and splits; adding to a position never loosens it. A buy that would fill at or below its own stop is not made: the stop was set from the last close, and a session that opens through it has undone the reason for the trade. There is no intraday path, no partial fill and no fee, so results are kinder than a real market's.

The gate checks its limits against the last close, and orders fill at the next open. A gap can therefore leave a position somewhat larger, or its risk to the stop somewhat greater, than what was approved. A buy the cash cannot pay for is rejected whole.

Splits: the price source restates all earlier prices after a split, so positions and their orders are moved onto each new snapshot's share scale before anything is matched. The move is only made when the listed split and the restated prices agree; when they do not, the position is left unvalued, its orders wait, and the run is marked degraded. A position that syncs have found without a usable, consistent price for more than `PAPER_STALE_DAYS` days, such as a delisted one, is closed at the last close it could be matched to.

**When to run it.** After the close, once closing prices are published, and before the next session opens. Yahoo Finance can take hours after the close to publish them; until then the run fails with a message saying so and can simply be repeated. Once the next session has opened the run is refused: its signals would rest on that day's news and prices, and would be filled at an open that has already passed. The same check is made again after the data is collected and before orders are placed. With a local 7B model a run takes five to eight minutes.

**Running it again.** A session that already has signals is skipped, so the command is safe to schedule and to repeat. A run that was killed or crashed is taken up where it stopped: the data it collected and every model answer it received were saved as they arrived, so nothing is fetched or asked twice, and every order has an id fixed by the decision behind it, so none is placed twice. While another process is still running the same session, the command reports `blocked` and exits non-zero, `--force` included. `--force` starts the session over from scratch.

Whenever a session is decided again, for whatever reason, the orders that stand are the ones the latest decision asks for: just before placing, anything an earlier attempt placed that is no longer wanted, or is wanted differently, is taken back. Setting the kill switch and running again therefore also takes back orders that were already placed for that session.

**When a run does not count.** A run fails, and places nothing, when the price source is down, when fewer than half the universe has usable prices for the session, when the next session is already trading, when the model is unreachable, or when the cost limit is reached. A run that finished but lost model calls, had outputs refused, had data missing, or could not analyse or price something the account holds is marked `degraded`.

**Checked citations.** The analyst names the data points it relied on (`rsi14`, `sma50`, `trailing_pe`, ...) with their values. Code compares each with the snapshot and records mismatches on the view, along with any figure in the text that appears nowhere in the data shown. The portfolio manager and the judge see these warnings.

**Running it every day (macOS).** `scripts/daily_run.sh` is one unattended run: it starts Ollama when the configured model needs it and it is not already up (and stops it again afterwards), keeps the Mac awake while the run goes, writes a log under `data_store/logs/`, and shows a notification saying how the run ended. Only one copy runs at a time, and stopping it stops the pipeline and the Ollama it started. When the pipeline has ended well, the same run goes on to the evaluation (`python -m evaluation --if-changed`, see [Evaluation](#evaluation)), so that the report the dashboard reads is written without anyone remembering to. That step is taken under the run's own lock, is stopped if it has not finished after `DAILY_EVALUATION_SECONDS` (600), and never changes how the run ended: a failure is logged and notified, and the next call tries again. `DAILY_EVALUATION=0` leaves it out. `scripts/schedule.sh` puts the run on a schedule through launchd:

```bash
scripts/schedule.sh install     # run every day at 18:00, 20:00, 22:00, 23:45 and 07:00, local time
scripts/schedule.sh status      # is it loaded, how did the last run end, the last log lines
scripts/schedule.sh run         # start one run now, the way the schedule would
scripts/schedule.sh uninstall   # stop the schedule; records and logs are untouched
```

The times were chosen for US Eastern and are this Mac's local time; set `DAILY_RUN_TIMES="HH:MM HH:MM ..."` before `install` to use others. There are several because closing prices can be published hours after the close: a run that comes too early fails and the next one tries again, and once a session has signals the rest are skipped. Three things to know:

- launchd does not wake a sleeping Mac. A run that was due while it slept is made once when it wakes. If that is after the next session has opened, the run is refused and that session is missed.
- macOS does not let scheduled jobs read the Desktop, Documents or Downloads folders. The project has to live somewhere else, and `install` refuses to go on if it does not.
- The first run creates the paper account with the settings in force at that moment.

**What is recorded.** Each run writes `signals.json` and `signals.md` under `data_store/runs/<session>/<run id>/`, and everything goes into the SQLite journal at `data_store/journal.sqlite3`: runs with their cost, every event (who said what to whom, in order, in the model's own words), macro views, and the paper account's positions, orders, fills and daily equity. The journal carries a format number, and a file written under another format is refused, not misread. It uses SQLite's write-ahead log, which adds `-wal` and `-shm` files beside it and needs a local disk: do not point `JOURNAL_PATH` at a network or synced folder.

**Comparing models.** Each `--arm` is a model configuration. All arms read the same data snapshot; each keeps its own signals and its own paper account.

Each stage of the larger plan has an exit test; `--status` shows where the signal stage's stands (five trading sessions in a row without error, with at least one sell or trim accepted along the way).

## The Desk: meetings through the trading day (paper trading)

```bash
python -m pipeline.desk tick        # one round: the watch, then a meeting if one is due
python -m pipeline.desk status      # today's meetings, the watch's last round, what is due now
python -m pipeline.desk watch       # only the watch
python -m pipeline.desk due         # print which meeting is due now, if any
python -m pipeline.desk meet        # hold the meeting that is due
```

The desk is a second group beside the daily pipeline, with a paper account of its own (its records are kept under a model configuration whose name starts with `desk/`). The daily pipeline is the baseline the desk is measured against, and adding the desk changed nothing in it: a test holds it to a fingerprint of every prompt, event and signal table taken before the desk was added. Rules changed since then were changed for both groups alike, so that the two stay comparable (on 2026-11-22: the macro view is formed every session, a holding the manager leaves out can be sold by default, and a sector fund counts towards its sector's limit). The fingerprint is taken with the macro view lasting a week, as it did then. Both use the same journal, and when the baseline has already collected a session's data the desk reads that same snapshot.

A short job runs `tick` every few minutes. A round does two things.

**The watch** is plain code and takes a few seconds: no model is called. On a trading day, from half an hour before the first check-in until the close, it fetches headlines for every name in the desk's book (holdings and orders waiting to fill) and the day's prices so far for the book, the S&P 500 and the VIX. Outside those hours it does nothing. What it fetches goes into the **ledger** (`data_store/ledger.sqlite3`), a file that is only ever added to: each story with the moment this system first had it, each price with the moment it was fetched, and a line for every fetch, the failed ones included. A fetch that failed is recorded as failed and nothing is put in its place. The prices come from the same source as the daily bars (Yahoo Finance, the day's bar while it is still forming) and may run behind the market.

**A meeting**, when one is due:

| Meeting | When (New York) | What it does |
|---------|-----------------|--------------|
| Check-in before the open (`premarket`) | `DESK_PREMARKET`, 08:30 | What has come in overnight. May take back a buy that is waiting to fill, or sell or trim a holding, at that day's open |
| Check-ins during the session (`midday`, `afternoon`) | `DESK_MIDDAY` 12:00, `DESK_AFTERNOON` 15:00 | What has come in since the last meeting. Nothing is traded: what is decided is noted for the close meeting |
| Check-in called by a plan (`called`) | While the watch is on, when a condition of one of the desk's plans that says to meet has held and no meeting has heard it. At most `DESK_CALLED_MAX` a day | A check-in like the others, with the name whose plan called it put before the analyst whether or not anything else is new (see "Plans" below) |
| Close meeting (`close`) | From two hours after the close until the next open | The daily pipeline's seven stages, reading its headlines from the ledger and told what the day's check-ins found. After the views, the quant engineer's seat gives the forecast engine's distributions (see the next section): recorded, and shown to no model. Then the desk's plans: what a plan's condition does by itself is carried out here, and every name the desk will hold gets or keeps its plan |

A **check-in** forms no new view. The news desk (code) says what is new for each name in the book since the last meeting: headlines first seen since then, and where the name is trading against its usual daily range (ATR). Where there is something to ask about, the analyst says what it does to the view it already holds (`unchanged`, `strengthened`, `weakened` or `broken`), naming the headlines it relied on, and the portfolio manager says what to do. Code then decides: a check-in can only reduce risk, and only where the analyst's update says the view is weakened or broken. It never opens or adds to a position. A check-in with nothing new calls no model at all. Before the open, exits are sized, put through the risk gate (kill switch included) and placed to fill at that open, and a waiting buy can be taken back. During the session the desk's orders would fill at the next open anyway, so the decision is noted and the close meeting decides with the full day's data; the analyst and the manager are shown those notes there.

**The book during the session.** The paper account books a fill at the close meeting, when the day's bar is complete, so between the open and that meeting the journal still lists an order placed for this open as waiting. A check-in held then does not take that at its word. A market order that was due to fill at the open is taken as filled: a buy counts as held, so cutting it can be noted for the close and it can no longer be taken back; a position that was to be sold there in full has left the book and is not reviewed; one sold in part is reviewed for the shares that are left. Both models are told this in so many words ("was due to fill at today's open": the broker can still turn a buy down, which shows at the close meeting), the record marks each such name (`at_open`), and the manager is shown, under each analyst update, the actions the checks will accept for that ticker.

**Every meeting has a cut-off**, and nothing the ledger first saw after it is shown to a model in that meeting. "First seen", not "published": a story is not known here before it has been fetched. The cut-off is recorded with the meeting, and each check-in records the headlines it showed, with the moment each was first seen.

**When a meeting does not count.** No check-in is due while the last session has no close meeting to build on (a desk on its first day, or a close meeting that never got through): it is not tried at all. A check-in fails, and decides nothing, when the model is needed and not reachable, or when the cost limit is reached; it is tried once more at the next round and then left. It is marked `degraded` when a fetch it relied on had failed, a model call was lost, or the open passed before its orders could be placed (then nothing is placed). A close meeting that fails is tried again an hour later, five times at most.

**Running it (macOS).** `scripts/desk_tick.sh` is one round. The watch needs no lock; a meeting is handed to `scripts/daily_run.sh`, so it takes the same lock as the daily pipeline, never runs a model beside it, and starts and stops Ollama the same way. The schedule is separate from the daily pipeline's:

```bash
scripts/schedule.sh install desk     # a round every 15 minutes (DESK_WATCH_MINUTES)
scripts/schedule.sh status desk
scripts/schedule.sh uninstall desk
```

While the watch is on, each round keeps the Mac from going to sleep on its own until the next one (`DESK_KEEP_AWAKE=0` turns that off). A closed lid still puts it to sleep, and launchd does not wake it: keep the Mac on with the lid open from before the open until after the close, or run the desk somewhere that stays on. The log is `data_store/logs/desk_<date>.log`.

**What the desk does not do yet.** Its meetings are held by the same figures as the daily pipeline, plus two seats that are code: the news desk and, at the close meeting, the quant engineer. The forecast is recorded and reconciled and bears on no order yet. Plans with triggers, and the analysts who look at people instead of prices, are later stages of `docs/UPGRADE_PLAN.md` (section 11). The headlines are titles only, and filings are not fetched yet.

## The Forecast Engine (plain code; it places and changes no order)

```bash
python -m forecast estimate   # estimate the coming session's parameters now (otherwise done at the week's first close meeting)
python -m forecast status     # the parameters on disk, and each group's forecasts and how they held up
python -m forecast verify     # work each group's latest forecast out again and compare it with the record
```

At the desk's close meeting, after the analyst's views, a seat that is code and calls no model (the quant engineer, `pipeline/quant.py`) says what the price of each name under discussion may do next. **A forecast is a distribution, not a number**: where half of the simulated paths end after 1, 5, 10 and 20 sessions, and nine in ten; how likely the stop is to be touched on the way; and for the book as it stands, what it may be worth, how likely two stops are to go on one day, and how likely the breakers are to trip. No method predicts a price. What can be had, and checked, is calibration: of the things said to be nine-in-ten, about nine in ten should happen. A model of prices offers no edge in direction; it gives the shape of the risk.

**It decides nothing.** What the seat says is recorded and goes no further: no model is shown it, and sizing, the risk gate and the orders are given exactly what they would have been given without it. A test runs three days of meetings with the forecast on and off and compares every prompt, event, signal table and order. A forecast that fails costs the meeting nothing: it is said in one line and the meeting goes on. `FORECAST_ENABLED=false` turns the seat off. The baseline group has no such seat.

**The model.**

- *The market's state.* The index is taken to be in one of a few hidden states (two: calm and turbulent), each with its own daily return, changing from day to day by a transition matrix. Both are estimated from twenty years of the S&P 500's daily bars (EM, from fixed starting points). Each day the probability of each state is carried on from the returns since. Where the history does not hold the states asked for (one freak day in an otherwise even history is not a second regime), one state fewer is estimated and the parameters say so.
- *Each name.* Its daily move is its share of the market's (beta), its share of its sector fund's own move, and a move of its own. The days of that last part are sorted into three kinds and each is counted once: the session that reacted to an earnings report (a report at or after its session's close, 13:00 on a half day, moves the next session; one dated on a day without a session moves the next there is), a jump (an own move beyond `FORECAST_JUMP_SIGMAS` of what was expected that day), and an ordinary day. A report day or a jump day is in neither the sample ordinary days are redrawn from nor the volatility estimate, and a report day is never counted as a jump: otherwise one large move would enter the forecast two or three times. A name's own average return counts for a fraction of itself (`FORECAST_DRIFT_SHRINK_DAYS`): a mean from a few hundred days is mostly noise.
- *The simulation.* 10,000 paths (`FORECAST_PATHS`) for twenty sessions. All names share the same market and sector paths, so they fall together as they do in fact. A name's own ordinary move is redrawn from its own past, scaled to its volatility today (an exponentially weighted average) and to the market's state on that path; with the frequency of its past jumps a day is a jump instead, drawn from those; and the session that reacts to its next report draws from its past report days (its sector's when it has too few of its own). A sector fund's own jumps are simulated the same way and reach its names. The next report is the one the snapshot names, or one made on the evening of the meeting's own day, which moves the very next session: that one is found among the report times kept with the parameters, since a snapshot only names reports dated after its session. Inside a day the price is taken to move continuously, apart from a jump or a report, which is taken at the open: that is what says whether a stop was touched between two closes. On a day that reaches both the stop and the target, the stop is taken to have come first.
- *The stop it is asked about* is the one on the books for a holding, and for a name not held the one a buy would get today: the analyst's when sizing would accept it, else the sizing rule's. No seat names a price target yet, so "target before stop" is asked of a notional one `FORECAST_TARGET_R` times as far above the close as the stop is below.

**Parameters change once a week.** Everything estimated from history is estimated on data up to the last session of the week before, and kept as one numbered file under `data_store/forecast/`. The first close meeting of a week makes the estimate if nobody has (`python -m forecast estimate` does it ahead of time, on a weekend say). Within a week only two things follow the new bars: the market's state and each name's volatility. So a forecast that changes during a week changes because of what happened, not because the estimate was run again. If the estimate fails, last week's is used and the meeting says so; with none at all, no forecast is made and the meeting says why. A version on disk is never written over: a forecast names the version it was made from.

**Worked out again, it is the same.** The seed is fixed by the meeting's id, and the snapshot keeps the daily bars of the index and the sector funds beside the names'. From the snapshot, the numbered parameters and the seed, `python -m forecast verify` reproduces a recorded forecast digit for digit.

**How a model's words become a number.** In one place only (`forecast/weights.py`), and by record. An analyst's bullish or bearish view tilts a name's drift only when views held with that confidence have a record: fewer than `FORECAST_TRACK_MIN` scored views, or a rate of beating the benchmark not more than `FORECAST_TRACK_Z` standard errors above one half, and the tilt is exactly zero. Above that the hit rate is first pulled towards one half and the tilt is capped at `FORECAST_TILT_CAP` of the name's volatility over the view's horizon. With the evidence a few weeks give, every tilt is zero, on purpose. (The same file holds the rule for a seat's stated probabilities of events, for the stages that will have such seats.)

**Every forecast is kept and set against what happened.** A forecast goes on record when its meeting is recorded as finished, and in the same save: a meeting that fails after the seat has spoken leaves no forecast behind, and the one a finished meeting of that session had left stays as it was. Each close meeting reconciles the forecasts whose horizon has ended, from that day's snapshot; `python -m evaluation` reconciles the rest. A forecast is measured by where the outcome fell in it: inside its middle 50%, 80% and 95% or not, and by a pinball loss over its percentiles; the stop-touch chance by a Brier score against whether a low reached the stop. Beside each stands **the plain forecast**: historical volatility and nothing else, no states, no jumps. The engine is worth keeping only where it does better, and the report and the dashboard show the two side by side. A score is worked out again until two sessions after the horizon ended, then it stands.

**What it says when it cannot.** A name the week's parameters do not hold (it joined the universe in mid-week, or has too little history) is named as not forecast, with the reason, until the next week's estimate takes it in: estimating again in mid-week would move every other name's parameters. So is a snapshot without the index's bars. Nothing is put in a forecast's place.

**Limits to know.**

- Past earnings dates come from the price source's earnings calendar page, which the client reads with the `lxml` package (in `requirements.txt`). Where a name's dates cannot be fetched (the package is missing, or the source refuses), its parameters say that its report days are not known: they are then not told apart from other days (the large ones count as jumps), and a report inside a forecast's horizon is said to be there and not modelled.
- The forecast is made at the close meeting only, not at check-ins. The book's forecast assumes no trading after today and is not reconciled; only the names' forecasts are. A holding the evening's snapshot has no usable price for stays in the book at the value the account carries it at, and the forecast's notes name it. (The first version left such a holding out, which put the book below its drawdown breaker before a day had passed. A forecast recorded by that version on an evening with such a holding does not work out again to the same book figures.)
- Names move together only through the market and their sector. A theme shared by a few names is not modelled, so several falling at once is somewhat underestimated. Volatility over the horizon changes only with the market's state.
- Forecasts made on the same day share that day's market: they are worth fewer than their count, and the report says how many days they come from.
- When a session is run again on purpose and that run fails, the forecast of the session's earlier, finished meeting stays on record and goes on being reconciled: it is what that meeting said.

## Working a Trade Back from Its Endings (it places and changes no order)

At the same meeting, right after its forecasts, the quant engineer reads the simulated paths a second time, backwards. A trade (a holding, or a name the analyst would own) has three endings within its horizon: **the target is reached first, the stop is touched first, or neither** by the time the horizon runs out. Every simulated path ends exactly one way. For each ending the seat says what the paths that end that way went through (`forecast/backward.py`):

- how often each ending comes, and how often the stop sells and the price still stands above today's close when the horizon ends ("right about the direction, shaken out on the way");
- the day that settled it, the deepest dip before it was settled, where a touched stop actually sold (below the stop where the price opened through it), and how much of the whole movement came on its single largest day;
- for each kind of event the simulation contains, **its chance given each ending**: of the paths that end at the stop, on what share did it happen first? The events are the name's own sharp moves, its sector's, an earnings report taken well or badly, the market spending a day in its other state, and the index moving `BACKWARD_MARKET_MOVE_PCT` percent from today's close. An event counts only when it happens on or before the day that settles the path: a jump after the stop has sold is no part of the trade.

The chance of an event given an ending is counted on the paths, and a test holds it to what Bayes' rule makes of the chance of the ending given the event. **The ratio** of an event's chance given the stop to its chance given the target says how much the event tells about where the trade is heading; the events are ranked by the log of that ratio with half a path added to each count, so that an event seen on a handful of paths does not outrank one seen on thousands. An ending on fewer than `BACKWARD_MIN_PATHS` paths is too rare to read, and that is said. All of it is true inside the simulation and no further: an event the simulation does not contain cannot appear.

**The scenario analyst** (a model's seat, `agents/ticker/scenario.py`) is there for what the simulation lacks. Once every decision of the meeting has been made, it is shown each name's facts and a numbered list of its headlines, and asked whether any headline points to something particular that has not happened yet and would move the price sharply on the day it does. What it proposes is judgement: a probability nobody measured, a window of days, a direction and a range of sizes. Code checks every event and repairs none (`pipeline/scenario.py`): it must cite a headline it was shown, lie within the trade's horizon, have a probability strictly between 0 and 1 and a move of at least `SCENARIO_MIN_MOVE_PCT` percent, and say how anyone could tell afterwards that it happened. A name the ledger holds no headline about is not asked about. The seat speaks after the risk gate for one reason: its model calls count against the run's spending limit like any other, and a seat that decides nothing must not be able to use up what a deciding seat needed.

**The rehearsal.** The same paths are then drawn again with the proposed events on them (each happens or not by its stated probability, on a day drawn evenly from its window, at a size drawn from the name's own past jumps, its sector's when it has few, and held inside the stated range; a range beyond the largest past jump is cut to it, and the meeting hears that), and read backwards again. **Two readings are kept apart**: the measured one, which rests on the simulation alone, and the rehearsed one, marked wherever it is shown as resting on judgement. The forecast on record is the measured one and does not move for anything the seat says: each event draws from a random stream of its own, and a test compares three days of meetings with the seat on and off, down to every other prompt, event, order and kept number.

**Scored when the days have passed** (`forecast/outcomes.py`). Once a trade's horizon has ended, the daily bars say which ending happened: the first day whose low reaches the stop or whose high reaches the target settles it, the stop first on a day that does both, as in the simulation, and the meeting's own day never counts. The three chances are scored against it (a Brier score: 0 for a certain and correct call, 2 for a certain and wrong one), for the measured reading, for the rehearsed one, and for **the plain forecast**, the textbook chances of a driftless walk between two barriers at the name's historical volatility. If the rehearsed reading does not score better than the measured one, the seat's events made the chances worse. Each proposed event is scored too, where code can tell whether it happened: a close beyond a level, a one-day move, or a whole word in a headline. A headline counts only if the ledger first saw it **after** the cut-off of the meeting that proposed the event: the headline an event was proposed from cannot be the proof that it happened. What decides that is the ledger's clock and not the source's date: a story dated an hour before the meeting that the ledger only came to see the next day was not known at the meeting. A headline belongs to the event's days by its source's date, from the close before the first day to the close of the last; one without a date belongs to them if the ledger first saw it by the first reading after that close (a name that is not held is read once an evening, by the close meeting). An event checked in words only is never scored.

**A headline check gets a verdict only from a reading without a gap** (`Ledger.read_between`). Saying that something did not appear takes a reading at or after the close of the event's last day, and none before it that may have missed something: a reading asks for the latest five headlines, and one that brings back a full page the ledger had seen none of may have let older ones go by. Without such a reading the event is scored neither way and waits, even when a matching headline was seen (what was seen is recorded beside it): scoring only the events seen to happen would tilt the record towards them. The same holds when the reading that closes the days comes more than eight hours after the last close (the wire was down that evening, or no meeting was held) and is the first to see a headline without a date: nobody can say which day that headline is from.

**Limits to know.**

- With the local 7B model the seat's events are mostly refused. Its first run on live data returned five events for one company, none of them from anything in the data; requiring a cited headline is what came of that. Whether the seat adds anything is for the scores to say.
- A proposed event is one day's move, independent of everything else. Bad news comes in clusters, and chains of events are for a later stage.
- A headline check is only as good as the ledger's reading: the watch's rounds for names in the book, and each close meeting's latest five for the names it discussed. For a name in the news every day, an evening's five are often all new, which is a gap: most headline checks on such names will wait and never be scored. The report says how many wait. "No gap" itself rests on the source handing out its latest headlines first.
- A headline without a date that the ledger first sees on the evening of an event's last day is counted as those days' news, though it may be a few hours younger.
- The words of a headline check are screened for the company's name, the ticker, words the headlines shown already have and a short list of common words, no further. A word as broad as a publisher's name is still taken, and makes its event easier to score as having happened.
- An event on a name that drops out of the meeting's candidates and is not held gets no further reading, and waits for good. Reading the names that have an event open is a job for the watch, and is not built.
- The trades are worked back at the close meeting only, and nothing here is shown on the dashboard beyond the lines as they were said.

## Plans: what the desk is held to (these change what the desk trades)

Everything in the two sections above is recorded and decides nothing. This section is different: **code sells on it.** It applies to the desk only. The daily pipeline has no plans and is exactly what it was.

**A plan for every trade** (`pipeline/plans.py`). Every position the desk will hold after a close meeting, and every buy that meeting places, has a plan: the thesis, the stop, the day by which the trade should have worked, and a short list of "if this, then that". The portfolio manager drafts it, one model call a name (`agents/ticker/planner.py`), once every decision of the meeting has been made and before the orders are placed. Code checks the draft and repairs nothing. A condition that fails a check is refused and the meeting hears why. A draft that does not say what would show the plan to be wrong, as a price to fall to, an average to close below or a day, with selling as its action, is refused whole, and the desk's own plan stands in: the stop that is on the books and the clock. So a buy is never left without a plan, and a model that cannot write one does not stop the desk from trading.

**What a plan may watch for, and what it may do, are two closed lists.**

| A condition | Checked against | May set off by itself |
|-------------|-----------------|-----------------------|
| The price reaches a level (`price_below`, `price_above`) | The last price a round of the watch fetched; at the close meeting, the day's low and high | Yes |
| A close below the 20-, 50- or 200-day average | The session's close and the snapshot's average, at the close meeting | Yes |
| A day (`date`) | The close of that session | Yes |
| One of the simulation's chances for the trade's three endings (`chance_below`, `chance_above`) | The close meeting's own reading of that trade | Only with `PLAN_CHANCE_ACTS=true`. As shipped it calls a meeting |
| A word in a later headline (`headline`) | Headlines the ledger first saw after the plan's version was made, by whole word | Never. It calls a meeting |

The actions are `exit` (sell the position), `trim` (sell half), `withdraw` (take back a buy that has not filled), `tighten_stop` (raise the stop) and `call_meeting`. **There is no action that buys or adds**, and a model cannot write one: the field is an enumeration, and a draft that names anything else is not a draft. The first four are what code carries out by itself, and each takes risk off. A headline is outside text, so it can only call a meeting: otherwise one made-up story could sell the desk out at the lows. The simulation's chances are the desk's own arithmetic, but a forecast earns the right to sell by being reconciled against what happened, and that record is a few days old: until the setting says otherwise they call a meeting too.

**A price has to be a real move away.** A price in a condition must lie at least one usual daily range from the last close: the desk's own measure of a move worth a look, `DESK_MOVE_ATR` times the 14-day ATR, or `DESK_MOVE_PCT` where the range is unknown. A stop a plan would raise must lie at least that far below the price that raises it. Nearer than that, an ordinary day reaches it, and the condition says nothing. The manager is told the rule and how large a usual day is, and a draft that ignores it is refused. It is not told which prices would pass: handed the nearest ones it could name, the local model named exactly those, in eight plans out of nine, and a plan made of the checks' own bounds is nobody's judgement of the trade. This rule and the one before it (what shows a plan wrong is a fall, an average or a day, never a rise) came out of the first trial on real data: the local model's plans passed every check there was then, and would have sold one position when it rose a seventh of a percent, calling that "the plan was wrong", and another on the first fall below the day's close.

**Every plan has a clock.** The day its time runs out is fixed when the plan is first made, from the analyst's horizon, and no later version can move it: "has this trade worked by the day we gave it" would mean nothing if the day moved every evening. A time stop is for a trade that has not worked. So when the time runs out with the position still held, a position that is not ahead of its cost is sold, and one that is ahead is not sold by the clock: it is put before the meeting, its plan is looked at again, and the clock does not strike a second time (`PLAN_TIME_STOP=exit_unless_ahead`, as shipped). The other settings are `exit` (sold, ahead or not), `call_meeting` (never sold by the clock) and `off`.

**Found during the session, carried out at the close.** Each round of the watch looks at the standing plans and records the conditions that hold for the first time: a price by the last one it fetched (once the session is trading), a headline by what the ledger has first seen since the plan was made. It records what was seen and when, and does nothing else. The desk's orders fill at the next open, so a sale placed at that moment and one placed at the evening's meeting get the same price, and the meeting has the day's whole bar. At the close meeting code takes up what the watch found, finds for itself what needs the finished bar (the day's low and high, the close against an average, a day reached, a chance crossed), and for each condition that may act by itself:

- **A sale** (`exit`, `trim`) is put among the meeting's proposals in the plan's own name and stands whatever the manager said of the name that day, unless the manager's own accepted decision already takes at least as much risk off. It is then sized and put through the same risk gate as every order, kill switch included, and placed to fill at the next open. A sale the gate turns back (trading is halted, say) stays open and is tried again at the next close meeting.
- **A stop** is raised only to a price above the one on the books and below the day's close. Raised to where the price already is, it would sell at the next open, which is not what tightening a stop is for: that is refused and said. On a day the data has no close for the name, it waits for the next meeting. The plan's next version names the new stop.
- **A buy** is taken back only while it has not filled. Since buys fill at the next open, in practice only an order the broker could not price is ever still waiting.

A condition that calls a meeting makes a check-in due while the watch is on, at most `DESK_CALLED_MAX` times a day, unless a meeting on the timetable is due anyway: that one hears it instead. The name is put before the analyst with what held, whether or not anything else is new, and the manager is shown it too. After the close, the close meeting hears it. Heard is all that happens by code: what to do is the meeting's to decide, by the rules every meeting has.

**A plan is not rewritten every evening.** It is looked at again when the analyst's view of the name has turned, when the stop on the books has moved, or when one of its own conditions has held. Then the manager is shown the plan that stands and why it is before it, and its draft becomes the next version. Each version records what code found changed, the manager's own sentence on it, and, worked out by code, what differs from the version before. A condition fires once in a plan's life, however many versions keep it and however it is worded. When the position is gone the plan is over, and says how: sold at its stop, sold at the open, or a buy that never filled.

**Two more rules of the desk's own**, both code: no position is opened when the name's earnings report is due within `PLAN_NO_ENTRY_BEFORE_REPORT` sessions (2; 0 turns it off), and a position is never added to (the pipeline never has: a position is sized once, when it is opened).

**Nothing half done.** What a meeting works out about plans goes on record as the meeting is recorded as finished, in the same transaction, together with the stops it raises: a meeting that fails leaves the plans, the triggers and the stops as it found them. A close meeting held again for a session works from where the first one did and leaves one of everything. If the plans cannot be checked or brought up to date, the meeting says so, is marked degraded, and goes on: the manager's own decisions are still placed.

**Limits to know.**

- With the local 7B model, expect the desk's own plan for nearly every name. Five trials on copies of the real records: asked for a number in a field of its own per kind of condition, it left the number out, ten plans of ten; with one required number, four passed and were plans to sell on an ordinary day's move; told the prices that would pass, it wrote those prices back; told the rule and left to choose, as the code now does, it wrote ten plans and none passed. The checks keep such plans out, the record says which plan is whose, and the seat needs a stronger model to be worth its calls.
- The target in a plan is the forecast's notional one. Nobody names a target yet, and reaching it sets nothing off unless the manager writes a condition for it.
- No model is shown the simulation's chances, so a manager that writes a condition on one picks its level blind. A level that holds already is refused.
- The chances are worked out once a day, at the close meeting. They are not worked out again at each round of the watch.
- A price is judged during the session by the last price the watch fetched, every fifteen minutes or so and possibly behind the market, and at the close by the day's low and high. Either way the sale fills at the next open, which may be far from the level that set it off.
- The watch reads headlines for names in the book only. Regulatory filings are not read at all, so there is no condition on one.
- Nothing stops the desk from buying back, at a later meeting, a name its plan has just sold.
- `withdraw` is in the list for completeness and for the day orders can wait; as things are, nothing sets it off before a buy has filled.
- Whether code carries a condition out is asked each time one is to be acted on, of the plan's record and of the settings as they stand, and never taken from the record alone: a record that marks a headline as acting by itself still only calls a meeting. Turning `PLAN_CHANCE_ACTS` off stops every chance on record from selling at once; turning it on applies to plans made after. A setting changed later does not otherwise rewrite plans on record.

## Evaluation

```bash
python -m evaluation                # every model configuration in the journal
python -m evaluation --lessons      # also have each model review a few of its own views
python -m evaluation --if-changed   # only when something has been recorded since the last report
```

**On the schedule.** Every scheduled run of the daily pipeline that ends well is followed by `python -m evaluation --if-changed` (see "Running it every day" above), several times a day. Asked that way, the evaluation measures only when there is something new to measure: when there is no report yet for the last completed session (a session has closed since the last one), when a run or a desk meeting has ended after the report was written, or when the report does not hold every model configuration, or cannot be read. Otherwise it says the report is up to date and fetches nothing. On an ordinary evening that is two evaluations: one after the daily pipeline, and one at the next call, which takes in the desk's close meeting held in between. A desk meeting is not itself followed by the evaluation. Lessons are never written on the schedule: they use the model, and are asked for by hand.

The evaluation measures what the pipeline's views and trades turned out to be worth, from prices that came after each decision. It reads the journal and writes a report to `data_store/evaluation/<date>/` (`evaluation.md` and `evaluation.json`). It decides nothing: it places no order and changes nothing the pipeline has recorded. What it adds to the journal are the scores, for the dashboard to read, and lessons when they are asked for. If the price source returns nothing for the benchmark, it stops with an error and leaves the scores on record as they were.

**Every view is scored, not only the trades.** A view names a direction and a horizon, and both can be checked, which gives far more evidence than the few trades do. A view is measured from the open of the session after it was written (the first price anyone could act on: the view is written after a close, with that evening's news already in hand) to the close of the session that ends its own horizon, dividends included. A bullish view is right when the ticker rose, a bearish one when it fell (a ticker that ends exactly where it started is right for neither); because almost every bullish view is "right" in a rising market, each is also checked against the benchmark (`EVALUATION_BENCHMARK`, SPY). Neutral views claim no direction and are counted apart. A score becomes final once it has been worked out two sessions after the view matured; until then it is worked out again each time, because a close read minutes after the bell can change and a dividend can be posted late. A final score is kept even if the source later drops the ticker, and a score measured against another benchmark is never kept. The scorecard splits the results by the confidence stated (does a higher confidence mean anything?), by direction, sector, the macro view of the day, and candidate against holding. Views written on one day rise and fall together, so they are worth less than their number; the report says how many sessions they come from and how wide the margin of error is.

**Three references, all plain code:**

- The benchmark bought and held.
- The day's candidates in equal weight, swapped each morning for the new list: what the screen alone is worth, before any model has an opinion. Like the benchmark it is fully invested, which the account never is, so both are references and not like-for-like rivals.
- Random picks from the same candidates. Each of the account's buys is replaced, `EVALUATION_SIMULATIONS` times, by a ticker drawn at random from that day's candidates the analyst gave a view on (without a view the pipeline cannot buy a ticker, so a random pick may not either; which way the view pointed does not matter). The account's own ticker and every other such candidate go through one and the same replay: sized by the sizing rule and the risk gate against the account as it stood that day, bought at the same open, protected by the rule's own stop, sold when the account sold its own pick, and valued on the day the account's books stand at. The ticker is what differs, so the share of random outcomes the account's picks beat answers one question: were the model's picks better than picking at random from the same list? About 50% means no. One thing a random pick inherits is not about itself: the moment it is sold was decided by what happened to the account's ticker. What the account's books recorded for the same buys is reported beside it and is not what is compared: it also reflects a stop the analyst proposed, a size cut by another order of the same day, and the limits then in force. Each candidate is gated on its own, with today's limits; the other orders of that day are not counted. The replay follows the paper broker's order of events, and tests hold it to the broker's recorded result.

**Trades** are put back together from the account's fills: what each made, how it ended (sold, stopped out, still open), and where the profit and loss came from by ticker, sector and kind of exit.

**Lessons.** With `--lessons`, the model reviews a few of its own views with the real outcome beside them: first the views the desk traded on, then the ones that were most confident and wrong. The facts in a lesson (what the price and the benchmark did) are written by code; only the assessment and the one-sentence lesson are the model's. The reviewer is told nothing that happened after the view matured. A lesson is kept but marked, and never shown to anyone, when it states figures the reviewer was not shown, when the one-sentence lesson names any figure at all (it is asked to be general, and would otherwise carry one ticker's numbers into the view of another), or when it runs past 300 characters. Lessons are stored in the journal. **They are shown to the ticker analyst only when `PIPELINE_USE_LESSONS=true`**, which is off by default, because that changes what the analyst reads and so what the pipeline decides. When it is on, the analyst sees up to `PIPELINE_LESSONS_PER_TICKER` lessons, about the same ticker first and then the same sector, and never one whose outcome was not yet known on the day of the decision. The analyst's own figure check then accepts the numbers in those lessons' fact lines, which may be about another ticker of the sector. A small local model writes generic lessons and sometimes misreads the facts it was given; read a few before turning this on.

**Forecasts.** For a group that makes them (the desk), the evaluation reconciles every forecast whose horizon has ended and reports, by horizon, how often the outcome fell inside the forecast's middle 50%, 80% and 95%, its pinball loss and the Brier score of its stop-touch chances, each beside the plain forecast's. See the section above.

**What it cannot tell you.** With tens of views or trades, a hit rate cannot be told from a coin flip: the margin is about 0.5/√n, 7 points at 50 and 5 at 100. The report states this beside its numbers. It can show that something is clearly wrong; it cannot show a durable edge in a few weeks.

A journal written before the evaluation existed opens as it is: the two tables the evaluation adds are created the first time it is opened, and nothing already in it is touched.

## Dashboard

```bash
python -m dashboard                 # then open http://127.0.0.1:8765
```

A web page over the journal, the data snapshots the runs recorded and the last evaluation report. Its own labels are in Chinese, and it has one look: a dark trading terminal.

**It is read-only.** Every address it serves answers a read and refuses anything else, the journal is opened in SQLite's read-only mode, and the page has no control that places an order, moves a limit or starts a run. It never writes to the journal and does not create one that is missing. The desk's ledger is read the same way, and only for the watch's last round; it is looked for beside the journal (`--ledger` names another), and one that is missing is not created. (SQLite may put its own empty `-wal` and `-shm` files beside a journal that is opened for reading.)

**It opens on what the desk is at now.** With a desk running, the meeting room shows the meeting that is going, live, or the last one that was held, whether a check-in or a close meeting. Above the room a strip lists the day's meetings in the order they were held, each with its time and how it stands; choosing one opens it, and a button leads back to now. Under them is the watch's last round: when it was, what it fetched and could not fetch, the largest move of the day in the book, and the next meeting on the timetable. The strip is read again once a minute. A meeting's heading gives its cut-off. For a model configuration with no desk the strip shows the day's one meeting, its close run, and no watch line.

**A check-in is shown like any meeting.** Its lines are laid on the same decision trail: the analyst's update where a view would be, the manager's decision where a proposal would be, and as the last step an order placed, a waiting buy taken back, or a decision noted for the close. The market screen shows the chart of the name being spoken of, from the data of the close meeting the check-in built on, with a line at the price the news desk reported for the day, from the line that reported it on.

**Who can see it.** The page has no login. By default it listens on this computer only (`DASHBOARD_HOST`) and answers only a browser that addresses it as `127.0.0.1` or `localhost`, so a page on another web site cannot read the records through your browser by pointing its own name at your computer. Set `DASHBOARD_HOST` to one of the computer's own addresses and it also answers under that address; set it to `0.0.0.0` and it answers anyone who can reach the computer, under any name. Everyone who can open the page sees every record, so do not put it on a network you do not trust.

**Two languages.** The page is written in Chinese and can be read in English: a switch at the right of the header changes the language, the choice is remembered in the browser, and the page loads again in it on the tab it was on. Every Chinese string of the page's scripts goes through one lookup (`t` in `dashboard/static/js/i18n.js`), which hands the string back as it is in Chinese and looks it up in `dashboard/static/js/en.js` in English; the names the server sends (seats, stages, groups) and the tallies it composes are put into English where its answers arrive. Recorded words are never looked up: what a model or a rule said is shown as it was written, in either language. Two tests keep the two in step: a string written without the lookup, or one with no English on record, fails `tests/test_dashboard_i18n.py`.

**Only what was recorded is shown.** What a model said appears word for word, in the language it was written in. The page neither writes nor polishes a line, and puts recorded text on the page as text, never as markup. Fixed vocabulary (a side, a status, a stage) is named in Chinese; anything a model or a rule wrote in its own words is left as it is. The signs over the figures, the decision trail and its counts are read off the record's own fields (a direction, a verdict, a number of shares) or are counts of recorded lines; none of them is written by a model. They read the record the way the pipeline does: a ticker in the portfolio manager's list is trimmed and put in capitals, and only the first proposal for a ticker counts, with any later one noted as a repeat that does not.

**Nothing ahead of the line being played.** While a session is played or a run is going, the signs and the trail show what the record holds up to that line. A ticker nobody has mentioned yet has no row, and a step that has not happened has no cell, label or tooltip. A ticker is said to have got no further only at the last line of a run that has finished, or when the final result is asked for.

**Market data is the run's own snapshot.** Every price, bar, indicator and market series on the page is read from the data snapshot a run recorded, which is what its agents were shown: daily bars, not live quotes, and nothing is fetched from outside. The file is only read. While a run is still going, its snapshot is found through the session's data checkpoint, since the run writes down where it is only when it ends; a snapshot captured after that run began is not taken for its own, because another invocation may have saved its data over the checkpoint. A run that is no longer going and never wrote it down (it failed before it had data, or was killed) has no market data to show: the checkpoint belongs to the session, and by then it may hold the data of the run that replaced it. A stop or a limit is drawn on a ticker's chart when the line that set it has been reached. What the price did after a session comes from later sessions' snapshots and is joined on only once the record is over, marked as something nobody in that run could see; a later snapshot is used only when it has the same close for the session's day, so prices from before and after a split are never put on one chart. Direction is never told by colour alone: a rise or a fall carries an arrow, and a rising candle is hollow where a falling one is filled.

**Movement changes nothing.** Words are revealed a few characters at a time but are the recorded words at every moment, and a figure that runs up always ends on its value. A reader whose system asks for reduced motion gets everything at once, without animation, and a tape that stands still.

| Page | What it shows |
|------|---------------|
| 总览 | Equity beside the benchmark bought and held and the candidates in equal weight (both lines are read from the last evaluation report; the third reference, random picks, is a distribution and is on the evaluation page). Cash, the day's change, the fall from the high. The decisions of the latest close meeting. The two circuit breakers, judged with the risk gate's own comparisons. The kill switch, put to the gate's own function: that is the switch as the dashboard sees it at that moment, which covers the switch file but not a `TRADING_HALT` set for the pipeline alone, so what the last finished run itself recorded about being halted is shown beside it. |
| 会议室 | The main page. Fourteen figures stand round a table: seven macro analysts, the ticker analyst, the bull and the bear facing each other, the judge, the portfolio manager, and the risk officer and the trader, who are rules and are marked as rules. They have positions, not names, and are drawn by code; none is modelled on a real person. They are drawn as people at work: dark suits that take only a tint of the seat's colour, a shirt, one accent in that colour (a tie, a pocket square, a lanyard), and no face, since a drawn smile would say something about a line that the record does not; while one speaks, a small meter moves beside its head. Each carries the mark of its field. At the front of the room is the reader's own place, the lead's desk: the page is laid out as a team reporting to whoever leads it, and a line said to everyone lights that desk too. On the wall behind the figures is the market they are deciding in: one thin line per ticker of the snapshot, its recorded closes over the last sixty sessions, all drawn from one starting level, so that how far they have moved apart can be seen, with the ticker being talked about picked out and labelled. A strip along the top of the room gives the hour and the weather: the New York time of the line being played, the cut-off of what the team knew, the session the prices are from, the VIX, how many of the tickers rose and fell over those sixty sessions and the two furthest apart, the usual daily swing of the ticker in hand, and, once the quant engineer has said it, the chance of each market state. Above the room, a card says where the meeting ended, whatever line is being played: the macro view, how many tickers got how far (view, ruling, proposal, checks, risk gate, order), and each ticker's outcome in the trail's own words, sorted into what was acted on, what was turned back, what was kept as it is and what got no further than talk; an outcome leads to the line it was said at. The latest session is played in the order it was recorded. A row of arrows along the top of the room names the stages the meeting went through, lights the one being played and fills it as its lines are said. A light rests on whoever speaks. Each line is thrown across the room from its speaker to the one it was said to, in the colour the speaker wears, and the listener's ring flashes as it lands; a line said to everyone goes out as a wave that each figure answers in turn, the nearest first. (A stream still runs along the table's edge between the two.) A speaker with no figure of its own (the pipeline's own voice, the news desk, the quant engineer, the scenario analyst) speaks through the market screen, which says who it is and whether it is a model or code. The words appear on the line under the room, with the listener's face beside the speaker's, and a sign over each figure says what it has concluded so far. In the middle of the table stands the market screen, which follows the discussion: a ticker's daily candles, volume, recorded averages and usual daily swing (ATR) while that ticker is talked about, with the stops and limits the record puts on it; the tickers of the portfolio manager's list side by side; the recorded series of a macro analyst's field while that analyst speaks (the yield curve for the bond analyst, the sector funds for the equity analyst); otherwise the market as a whole. Each market series there is a tile with a small line of where it has been: its recorded daily closes where the snapshot kept them (the index and the sector funds), drawn solid, and otherwise the four values the snapshot does give (the session's own, and one, five and twenty-one sessions back), joined by a dashed line with a mark on each, so that the straight stretches between them are not taken for the path it took. Beside the room, a map shows who has spoken to whom by the line being played: the figures where they stand round the table, a line between two of them once one has spoken to the other, heavier the more lines there have been, and the line being played lit. A panel shows whoever is speaking, or a figure that was picked: its field, what it has concluded so far in its recorded fields and its own words, the figures it cited with the pipeline's own check of each against the snapshot (which allows about two per cent; where the two numbers differ, both are shown), the recorded data of its field, and how often it has spoken by that line. The whole session's tally for a figure is shown only once the record is over. A table lists the team as its lead sees it: every seat in its group, what it has concluded by the line being played, and how often it has spoken. Under the room, a board shows the debate over the ticker in hand round by round, the bull's line beside the bear's with the evidence each called its strongest, then the judge's ruling and what became of it. A ticker's chart carries a line of figures as a trading terminal does: the session's open, high, low and close, its change from the close before and its volume, following the pointer from day to day, with the months marked along the bottom. Under that, the decision trail has one row per ticker and one cell per step it went through (view, debate, ruling, proposal, check, risk, order). A row appears when its ticker is first spoken of and a cell when its step has happened, with a count of how many tickers have got how far; a cell leads back to the line that was said, and a button shows the final result at once. Everything else recorded with the current line is listed below that. While a run is going, new lines appear by themselves. The left and right arrow keys step through the record a line at a time and the space bar plays or pauses it; the transcript beside the room gives each line its time and who it was said to. |
| 决策 | How decisions changed from session to session: every ticker against every finished session, with the analyst's direction and confidence, whether it was held, what was proposed and what became of it, and a mark where the direction differs from the session before. It is read from each session's signal table, where an order is one that passed the risk gate (风控放行); fills are on the next page. Under it, the decision trail of whichever session is picked, read from the same run as that session's column; a cell of either leads to the replay of that run. |
| 持仓和订单 | Positions with their stops, orders waiting to fill, fills, and the last finished session's candidates, views, proposals and risk decisions. For the desk, the plans (作战计划): each plan that stands with its version, who wrote it, its stop and the day its time runs out; its conditions, each with what it does, whether code does it or a meeting decides, and where it stands (not triggered yet, triggered and waiting, carried out, not carried out, heard at a meeting), with the recorded reason, what was seen and what was done, verbatim; the versions newest first, each with why the plan was looked at again, what changed and what of the draft was refused; and the plans that are over, with how each ended. A sale a plan put in is marked as the plan's on the decision trail (按计划卖出), and a check-in a plan called is listed among the day's meetings (临时碰头). |
| 评估 | The last evaluation report as it was written: model configurations side by side, the scorecard, stated confidence against the hit rate, trades, where the account falls among random picks, model usage, and the lessons on record. The dashboard never runs the evaluation; `python -m evaluation` refreshes it. For a group that makes forecasts, two more cards are read from the journal itself, with or without a report: how the forecasts held up (the share of outcomes inside each band against the share there should be, for the engine and for the plain forecast, with the table of every figure under the bars), and what the engine last said (the market's state, each name's bands, stop and chance of touching it, the book, and what could not be forecast, in the recorded words). |
| 回放 | Any past day, played back the same way: pick a day and its close meeting is shown (its last check-in while there is none yet); the day's other meetings are on the strip above the room. |
| 运行状态 | Every run with its duration, model calls, cost, warnings and errors, and a link that replays that one run, a failed or replaced attempt included. |

**The quant engineer in the room.** Its lines are played like any other, in a stage of their own (预测) between the views and the debates, marked as code and not a model; it has no figure at the table yet. From the line that gives a name's forecast on, that name's chart carries the forecast to the right of the session's close: a fan that holds half of the simulated paths, a wider one that holds nine in ten, and the median as a broken line, with a mark at each horizon that reads out the recorded percentiles. It is drawn under the candles, so that once the record is over, what the price really did lies over what was forecast. Not a line earlier, like every other mark on the chart.

Along the top of every page runs a tape of the latest session's snapshot: each ticker with its close, its day's move, a small line of its recent closes and its usual daily swing, then the market series. On the meeting room and the replay it shows the prices of the run in the room instead, so an earlier session is never played under a later session's prices. It stops under the pointer, or with its button.

When a session was run more than once, the run shown for it is the one still going, else the one whose signals stand, else the last attempt.

A run counts as going only while the process that started it exists. One that was killed never records its ending, so the journal goes on saying "running" until the pipeline next takes that session up; the dashboard shows such a run as interrupted (中断), not as live. That label is the dashboard's own reading, not a status in the journal. If the page loses its connection to the dashboard it says so at the top, keeps what it last read, and reconnects by itself.

The server is Starlette with a server-sent event stream that looks at the journal once a second. The page is plain HTML, CSS and JavaScript modules: no build step, and nothing loaded from outside this computer.

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
| `PIPELINE_MACRO_MAX_AGE_DAYS` | `1` | The macro view is reused until it is this many days old. `1`: formed anew every session (seven more model calls a day for each model configuration). It was `7` until 2026-11-22 |
| `PIPELINE_DEBATE_TOP` | `3` | New candidates debated each day |
| `PIPELINE_DEBATE_ROUNDS` | `1` | Rounds per debate |
| `PIPELINE_MAX_COST_USD` | `2.0` | A run stops once its model calls have cost this much |
| `PAPER_INITIAL_CASH` | `100000` | Cash a new paper account starts with |
| `PAPER_SLIPPAGE_BPS` | `5` | Every market and stop fill is worse by this many basis points |
| `PAPER_STALE_DAYS` | `10` | An order with no usable price is cancelled after this many days, and a position with none is closed |
| `SIZING_RISK_PCT` | `1.0` | Percent of equity risked on a new position at full confidence |
| `SIZING_STOP_ATR_MULTIPLE` | `2.0` | Without a usable stop from the analyst, the protective stop is this many ATRs below the last close |
| `SIZING_MIN_STOP_PCT` | `1.0` | The stop is never closer to the price than this percent |
| `SIZING_STOP_MIN_ATR_MULTIPLE` | `1.0` | The analyst's own stop is used only when it is at least this many ATRs below the last close |
| `SIZING_STOP_MAX_ATR_MULTIPLE` | `4.0` | And at most this many |
| `RISK_MAX_POSITION_PCT` | `10` | Largest single position, percent of equity |
| `RISK_MAX_SECTOR_PCT` | `30` | Largest sector, percent of equity |
| `RISK_MAX_POSITIONS` | `10` | Most positions held at once |
| `RISK_MIN_CASH_PCT` | `10` | Cash kept back, percent of equity |
| `RISK_MAX_TRADE_RISK_PCT` | `1.0` | Largest loss on one new position if its stop is hit, percent of equity |
| `RISK_MAX_NEW_POSITIONS_PER_DAY` | `3` | Most positions opened in one day |
| `RISK_MAX_DAILY_TURNOVER_PCT` | `30` | Value of one day's orders, percent of equity |
| `RISK_DAILY_LOSS_LIMIT_PCT` | `3` | A day's loss at which new buying stops |
| `RISK_MAX_DRAWDOWN_PCT` | `10` | Drawdown from the high at which new buying stops |
| `RISK_MIN_PRICE` | `5` | Lowest share price the gate lets through |
| `RISK_MIN_DOLLAR_VOLUME` | `20000000` | Lowest 20-day average dollar volume the gate lets through |
| `RISK_MAX_LIMIT_DEVIATION_PCT` | `5` | Furthest a limit price may be from the last close |
| `KILL_SWITCH_FILE` | `data_store/HALT` | While this file exists no order is placed. `TRADING_HALT=1` does the same |
| `JOURNAL_PATH` | `data_store/journal.sqlite3` | The SQLite journal |
| `RUNS_DIR` | `data_store/runs` | Where signal tables are written |
| `EVALUATION_DIR` | `data_store/evaluation` | Where evaluation reports are written |
| `EVALUATION_BENCHMARK` | `SPY` | What views and the account are measured against |
| `EVALUATION_SIMULATIONS` | `1000` | Random-pick replacements of the account's buys |
| `EVALUATION_SEED` | `20261117` | Seed for those picks, so that an evaluation can be repeated |
| `EVALUATION_MAX_LESSONS` | `5` | Most lessons written by one evaluation run with `--lessons` |
| `PIPELINE_USE_LESSONS` | `false` | Show lessons to the ticker analyst. Changes what the pipeline decides |
| `PIPELINE_LESSONS_PER_TICKER` | `3` | Most lessons shown for one ticker |
| `LEDGER_PATH` | `data_store/ledger.sqlite3` | The desk's ledger: headlines and prices with the moment each was first seen |
| `DESK_PREMARKET` | `08:30` | The check-in before the open, New York time |
| `DESK_MIDDAY` / `DESK_AFTERNOON` | `12:00` / `15:00` | The check-ins during the session. One timed at or after the day's close is skipped that day |
| `DESK_WATCH_MINUTES` | `15` | How often the desk's job is scheduled (read by `scripts/schedule.sh install desk`) |
| `DESK_KEEP_AWAKE` | `1` | Keep the Mac from sleeping on its own between rounds while the watch is on |
| `DESK_CLOSE_DELAY_MINUTES` | `120` | How long after the close the close meeting is first tried |
| `DESK_RETRY_MINUTES` / `DESK_CLOSE_TRIES` | `60` / `5` | How soon, and how often, a failed close meeting is tried again |
| `DESK_CHECKIN_TRIES` | `2` | Tries at one check-in |
| `DESK_HEADLINE_DAYS` / `DESK_HEADLINES_SHOWN` | `7` / `8` | How long a headline is shown, and how many per ticker fit in a prompt |
| `DESK_MOVE_ATR` / `DESK_MOVE_PCT` | `1.0` / `3.0` | A move worth a look: this many usual daily ranges, or this many percent where the range is unknown |
| `FORECAST_ENABLED` | `true` | The quant engineer's seat at the desk's close meeting. It decides nothing either way |
| `FORECAST_DIR` | `data_store/forecast` | Where each week's parameters are kept, one numbered file per estimate |
| `FORECAST_PATHS` | `10000` | Paths simulated for every name at every meeting |
| `FORECAST_STATES` | `2` | States of the market: 2 is calm and turbulent, 3 adds crisis |
| `FORECAST_MARKET_YEARS` / `FORECAST_HISTORY_YEARS` | `20` / `5` | Years of daily bars read for the index's states, and for each name |
| `FORECAST_EWMA_LAMBDA` | `0.94` | Weight on the past in the exponentially weighted volatility |
| `FORECAST_JUMP_SIGMAS` | `4.0` | An own move beyond this many expected standard deviations is a jump, not an ordinary day |
| `FORECAST_MIN_EARNINGS` | `8` | With fewer past report days, a name's report moves are drawn from its sector's |
| `FORECAST_DRIFT_SHRINK_DAYS` | `5040` | A name's own average return counts for days / (days + this) of itself |
| `FORECAST_TARGET_R` | `2.0` | The notional target: this many times as far above the close as the stop is below |
| `FORECAST_TRACK_MIN` / `FORECAST_TRACK_Z` / `FORECAST_TILT_CAP` | `200` / `1.64` / `0.25` | How a record earns a say: the fewest scored cases, how far above a coin toss the hit rate must be, and the most an analyst's view may tilt the drift (as a share of the horizon's volatility) |
| `BACKWARD_MIN_PATHS` | `100` | An ending on fewer simulated paths than this is too rare to say what led to it |
| `BACKWARD_MARKET_MOVE_PCT` | `5.0` | "The index falls (or rises) this many percent from today's close" is one of the events looked for |
| `BACKWARD_REPORT_MOVES` | `1.0` | An earnings report "taken badly" or "well": the name opens at least this many of that day's expected moves away |
| `SCENARIO_ENABLED` | `true` | The scenario analyst's seat at the desk's close meeting. Off, there is no rehearsal; the trades are still worked back |
| `SCENARIO_MAX_EVENTS` / `SCENARIO_MAX_NAMES` | `5` / `8` | Events kept for one name, and names the seat is asked about at one meeting (one model call each) |
| `SCENARIO_MIN_MOVE_PCT` | `1.0` | The smallest one-day move that counts as an event |
| `PLAN_ENABLED` | `true` | The desk's plans. **On, code sells when a plan's condition says so.** Off, the desk keeps no plans, and the earnings-report rule goes with them |
| `PLAN_TIME_STOP` | `exit_unless_ahead` | What happens when a plan's time runs out with the position still held: `exit_unless_ahead` (sold at the next open unless it is ahead of its cost; ahead, it is put before the meeting), `exit` (sold, ahead or not), `call_meeting`, or `off` |
| `PLAN_CHANCE_ACTS` | `false` | Whether one of the simulation's chances may set off a sale. Off, such a condition can only call a meeting |
| `PLAN_MAX_CONDITIONS` | `5` | Conditions of the manager's own in one plan, beyond the one that says what would show it wrong |
| `PLAN_MAX_NAMES` | `10` | Names the manager is asked for a plan about at one meeting (one model call each). Beyond it the desk's own plan stands in |
| `PLAN_NO_ENTRY_BEFORE_REPORT` | `2` | No position is opened when the name's earnings report is due within this many sessions. `0` turns the rule off |
| `DESK_CALLED_MAX` | `2` | Check-ins a day that a plan's condition may call |
| `DASHBOARD_HOST` | `127.0.0.1` | Address the dashboard listens on. The default serves this computer only |
| `DASHBOARD_PORT` | `8765` | Port the dashboard listens on |

The pipeline's numbers, the risk limits included, are conservative placeholders, not recommendations. Set them before relying on any result.

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
├── scripts/               # daily_run.sh (one unattended run), desk_tick.sh (one round of the desk), schedule.sh (launchd, macOS)
├── tests/                 # pytest suite (no real model needed)
├── main.py                # CLI entry point (Rich terminal UI, preferences)
├── app.py                 # Streamlit web UI
├── requirements.txt       # Python dependencies
├── .env                   # API key (not committed)
├── .env.example           # Template for .env
├── forecast/
│   ├── regime.py          # The market's hidden states: a Markov chain estimated by EM, and its filter
│   ├── params.py          # The week's parameters: betas, ordinary / jump / report days, and the numbered files they are kept in
│   ├── engine.py          # The simulation: market, sectors and names together, and what is read off the paths
│   ├── weights.py         # How a record earns a say in the simulation: exactly zero until it has one
│   ├── scoring.py         # Reconciling forecasts with what happened, beside the plain forecast
│   ├── backward.py        # A trade's paths read backwards from its three endings: what each went through, and what tells them apart
│   ├── outcomes.py        # Which ending happened, and whether a proposed event did: the chances scored
│   └── __main__.py        # python -m forecast: estimate, status, verify
├── pipeline/
│   ├── daily.py           # The fixed daily pipeline (paper trading) and its command line
│   ├── desk.py            # The desk: the watch, which meeting is due, the close meeting that reads the ledger
│   ├── quant.py           # The quant engineer's seat at the close meeting: code that reads out the forecast and reconciles
│   ├── scenario.py        # What code keeps of the scenario analyst's events, and what it refuses
│   ├── plans.py           # The plan for each trade: what of a draft is kept, when a condition holds, what code may do about it
│   ├── checkin.py         # A check-in: what is new, what it does to a standing view, what may be done about it
│   ├── universe.py        # Ticker universe and the rule-based candidate screen
│   ├── macro.py           # Combines the macro agents' assessments by rule
│   ├── checks.py          # Rules between model output and what is kept
│   └── report.py          # Briefings and the Markdown signal table
├── portfolio/
│   └── sizing.py          # How many shares an accepted proposal becomes, and its stop
├── risk/
│   └── gate.py            # Hard limits on every order; kill switch
├── execution/
│   ├── broker.py          # What the pipeline needs from any broker
│   ├── paper.py           # Paper broker: matching against daily bars, splits, dividends, equity
│   └── placement.py       # Three-step order placement that survives a crash
├── journal/
│   └── store.py           # SQLite journal: runs, events, macro views, paper books, checkpoints, scores, lessons, the desk's meetings
├── evaluation/
│   ├── run.py             # The evaluation command: python -m evaluation
│   ├── scoring.py         # Scores every view against later prices; the scorecard
│   ├── trades.py          # The account's trades, put back together from its fills
│   ├── baselines.py       # Benchmark, candidates in equal weight, random picks from the same candidates
│   ├── lessons.py         # A model reviews its own views once the outcome is known
│   ├── memory.py          # Which lessons the ticker analyst is shown, when that is turned on
│   ├── prices.py          # Prices fetched once for the whole evaluation
│   └── report.py          # The evaluation as Markdown
├── dashboard/
│   ├── __main__.py        # The dashboard command: python -m dashboard
│   ├── app.py             # Read-only web server: GET addresses, the live event stream, the page's files
│   ├── reader.py          # Everything shown, read from the journal (opened read-only) and the last report
│   ├── personas.py        # The fourteen figures: position, seat, model or rule
│   └── static/            # The page: index.html, styles.css, js/ (meeting room, decision trail, playback, charts, pages)
├── agents/
│   ├── ticker/            # Ticker analyst, bull, bear, judge, portfolio manager (its proposals, and its plan for a trade), reviewer, scenario analyst, and the two voices of a check-in
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
│   ├── ledger.py          # The desk's ledger: what was seen and when it was first seen, fetches, the watch's rounds
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
│   ├── signals.py         # Pydantic models for the pipeline's signals
│   ├── trading.py         # Orders, fills, positions, accounts, risk decisions
│   └── evaluation.py      # View scores, scorecard, trades, benchmarks, lessons, the report
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

The dashboard page's own logic (playing a meeting back, following a run that is still going, how each decision came about, what the decision trail shows at each line and which run the 决策 page reads, what the market screen and the tape show and when, each figure's panel, words kept as text and revealed unchanged, number formats, seats, a check-in read off its record, the strip of the day's meetings, where a meeting ended and the stages it went through, who has spoken to whom, where a line is thrown from and to, what is drawn on the wall behind the room and said along its top, the team's table, the debate round by round, and a chart's line of figures) has tests in `tests/js`. Their stand-in for a browser refuses a page that would write the word "null" where a missing piece should simply be left out. `pytest` runs them with `node` when it is installed and skips them when it is not; `node --test tests/js/*.test.mjs` runs them alone. Two more tests hold the page to the pipeline: one sets the trail the page works out from a run's events beside the grid read from that run's signal table, for runs the real pipeline made, and requires them to agree; the other requires the page to read a ticker out of a proposal exactly as the pipeline's `strip().upper()` does. The code that draws the page (figures, charts, layout, animation) is checked by eye only.

## License

MIT
