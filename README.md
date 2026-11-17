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
| Macro | 7 models, then code | Each macro agent assesses the current state of its domain. Code combines the stances into the desk's macro view: the stance with the most confidence behind it, and sector tilts only where more than one agent's worth of confidence agrees. Reused for a week. |
| Views | 1 model | The ticker analyst writes one view per candidate and per holding: direction, confidence, horizon, invalidation, cited data and, for a candidate it is bullish on, the price it would put the stop at. |
| Debate | 3 models | Bull, bear and judge argue the strongest new candidates and any holding whose view has turned. |
| Proposals | 1 model | The portfolio manager proposes buy, sell, hold or trim. |
| Checks | code | Each proposal is accepted or rejected by rule. Every holding gets a decision; a missing one is "hold". |
| Risk | code | Each accepted proposal is sized, then the risk gate approves it, reduces it or rejects it, with the reason. |
| Orders | code | What passed is placed with the paper broker, to fill at the next session's open. |

**Models propose, code decides.** A model's output is a draft. Code checks it and keeps it, or refuses it and records why. Nothing is repaired silently: a confidence outside 0 to 1 is refused, not clamped. The rules for proposals:

- The ticker must be one of today's candidates or holdings. Only the first proposal for a ticker counts.
- A buy needs a checked analyst view of that ticker that is bullish, no ruling against it from the judge, a current macro view, and room in the book.
- Sell, trim and hold apply only to positions the account holds, and a position with an exit already waiting gets no second order.

These rules are also what limits the damage of a persuaded model. Headlines are outside text, and a model can be talked into things; code still only lets it buy a screened candidate that the analyst and the judge did not reject, with a stop inside a fixed range, the same money at risk wherever in that range the stop is, and limits it has no say in, or exit what the account already holds.

**Sizing.** A model says what and how sure; code says how much. A buy risks a fixed share of equity (`SIZING_RISK_PCT`, scaled by the lower of the manager's and the analyst's confidence) between the last close and a protective stop below it. The share count is that money divided by the distance to the stop as it will be placed, so a wider stop means fewer shares and the loss at the stop is never more than that money. The risk gate can then only cut the count further: with the placeholder limits, the single-position limit binds whenever the stop is closer than about a tenth of the price, and then a closer stop means less money at risk, not more shares. A sell exits the whole position, a trim half.

**Where the stop goes.** The analyst may name a stop for a candidate it is bullish on, with the level in the data it sits under. Code uses it only when it is between `SIZING_STOP_MIN_ATR_MULTIPLE` and `SIZING_STOP_MAX_ATR_MULTIPLE` ATRs below the last close, and never closer than `SIZING_MIN_STOP_PCT` of the price. A stop outside that range is refused, not moved to the nearest allowed price, and the view says so; the rest of the view stands. (A stop that is not a number at all fails the reply as a whole, like any other malformed field.) Figures in the reason are traced to the data like those in the thesis. A closer stop is hit more easily, on the entry day included, and a gap at the open costs more against it. With no usable stop from the analyst, the stop is `SIZING_STOP_ATR_MULTIPLE` ATRs below the close. The report shows, for every order, which of the two set its stop. A position already held keeps the stop it was opened with.

**The risk gate.** Every sized order is approved, reduced or rejected. All limits are settings, enforced in code, and every cut is recorded with the limit and the number that broke it:

- Long only. No position, no sell; a sell is cut to what is held.
- Whitelist, minimum price, minimum dollar volume, usable data.
- No buy without a protective stop below the price.
- Largest loss per trade if the stop is hit, largest single position, largest sector, minimum cash, most positions, most new positions in a day, most turnover in a day.
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

**Running it every day (macOS).** `scripts/daily_run.sh` is one unattended run: it starts Ollama when the configured model needs it and it is not already up (and stops it again afterwards), keeps the Mac awake while the run goes, writes a log under `data_store/logs/`, and shows a notification saying how the run ended. Only one copy runs at a time, and stopping it stops the pipeline and the Ollama it started. `scripts/schedule.sh` puts it on a schedule through launchd:

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

## Evaluation

```bash
python -m evaluation                # every model configuration in the journal
python -m evaluation --lessons      # also have each model review a few of its own views
```

The evaluation measures what the pipeline's views and trades turned out to be worth, from prices that came after each decision. It reads the journal and writes a report to `data_store/evaluation/<date>/` (`evaluation.md` and `evaluation.json`). It decides nothing: it places no order and changes nothing the pipeline has recorded. What it adds to the journal are the scores, for the dashboard to read, and lessons when they are asked for. If the price source returns nothing for the benchmark, it stops with an error and leaves the scores on record as they were.

**Every view is scored, not only the trades.** A view names a direction and a horizon, and both can be checked, which gives far more evidence than the few trades do. A view is measured from the open of the session after it was written (the first price anyone could act on: the view is written after a close, with that evening's news already in hand) to the close of the session that ends its own horizon, dividends included. A bullish view is right when the ticker rose, a bearish one when it fell (a ticker that ends exactly where it started is right for neither); because almost every bullish view is "right" in a rising market, each is also checked against the benchmark (`EVALUATION_BENCHMARK`, SPY). Neutral views claim no direction and are counted apart. A score becomes final once it has been worked out two sessions after the view matured; until then it is worked out again each time, because a close read minutes after the bell can change and a dividend can be posted late. A final score is kept even if the source later drops the ticker, and a score measured against another benchmark is never kept. The scorecard splits the results by the confidence stated (does a higher confidence mean anything?), by direction, sector, the macro view of the day, and candidate against holding. Views written on one day rise and fall together, so they are worth less than their number; the report says how many sessions they come from and how wide the margin of error is.

**Three references, all plain code:**

- The benchmark bought and held.
- The day's candidates in equal weight, swapped each morning for the new list: what the screen alone is worth, before any model has an opinion. Like the benchmark it is fully invested, which the account never is, so both are references and not like-for-like rivals.
- Random picks from the same candidates. Each of the account's buys is replaced, `EVALUATION_SIMULATIONS` times, by a ticker drawn at random from that day's candidates the analyst gave a view on (without a view the pipeline cannot buy a ticker, so a random pick may not either; which way the view pointed does not matter). The account's own ticker and every other such candidate go through one and the same replay: sized by the sizing rule and the risk gate against the account as it stood that day, bought at the same open, protected by the rule's own stop, sold when the account sold its own pick, and valued on the day the account's books stand at. The ticker is what differs, so the share of random outcomes the account's picks beat answers one question: were the model's picks better than picking at random from the same list? About 50% means no. One thing a random pick inherits is not about itself: the moment it is sold was decided by what happened to the account's ticker. What the account's books recorded for the same buys is reported beside it and is not what is compared: it also reflects a stop the analyst proposed, a size cut by another order of the same day, and the limits then in force. Each candidate is gated on its own, with today's limits; the other orders of that day are not counted. The replay follows the paper broker's order of events, and tests hold it to the broker's recorded result.

**Trades** are put back together from the account's fills: what each made, how it ended (sold, stopped out, still open), and where the profit and loss came from by ticker, sector and kind of exit.

**Lessons.** With `--lessons`, the model reviews a few of its own views with the real outcome beside them: first the views the desk traded on, then the ones that were most confident and wrong. The facts in a lesson (what the price and the benchmark did) are written by code; only the assessment and the one-sentence lesson are the model's. The reviewer is told nothing that happened after the view matured. A lesson is kept but marked, and never shown to anyone, when it states figures the reviewer was not shown, when the one-sentence lesson names any figure at all (it is asked to be general, and would otherwise carry one ticker's numbers into the view of another), or when it runs past 300 characters. Lessons are stored in the journal. **They are shown to the ticker analyst only when `PIPELINE_USE_LESSONS=true`**, which is off by default, because that changes what the analyst reads and so what the pipeline decides. When it is on, the analyst sees up to `PIPELINE_LESSONS_PER_TICKER` lessons, about the same ticker first and then the same sector, and never one whose outcome was not yet known on the day of the decision. The analyst's own figure check then accepts the numbers in those lessons' fact lines, which may be about another ticker of the sector. A small local model writes generic lessons and sometimes misreads the facts it was given; read a few before turning this on.

**What it cannot tell you.** With tens of views or trades, a hit rate cannot be told from a coin flip: the margin is about 0.5/√n, 7 points at 50 and 5 at 100. The report states this beside its numbers. It can show that something is clearly wrong; it cannot show a durable edge in a few weeks.

A journal written before the evaluation existed opens as it is: the two tables the evaluation adds are created the first time it is opened, and nothing already in it is touched.

## Dashboard

```bash
python -m dashboard                 # then open http://127.0.0.1:8765
```

A web page over the journal and the last evaluation report. Its own labels are in Chinese.

**It is read-only.** Every address it serves answers a read and refuses anything else, the journal is opened in SQLite's read-only mode, and the page has no control that places an order, moves a limit or starts a run. It never writes to the journal and does not create one that is missing. (SQLite may put its own empty `-wal` and `-shm` files beside a journal that is opened for reading.)

**Who can see it.** The page has no login. By default it listens on this computer only (`DASHBOARD_HOST`) and answers only a browser that addresses it as `127.0.0.1` or `localhost`, so a page on another web site cannot read the records through your browser by pointing its own name at your computer. Set `DASHBOARD_HOST` to one of the computer's own addresses and it also answers under that address; set it to `0.0.0.0` and it answers anyone who can reach the computer, under any name. Everyone who can open the page sees every record, so do not put it on a network you do not trust.

**Only what was recorded is shown.** What a model said appears word for word, in the language it was written in. The page neither writes nor polishes a line, and puts recorded text on the page as text, never as markup. Fixed vocabulary (a side, a status, a stage) is named in Chinese; anything a model or a rule wrote in its own words is left as it is.

| Page | What it shows |
|------|---------------|
| 总览 | Equity beside the benchmark bought and held and the candidates in equal weight (both lines are read from the last evaluation report; the third reference, random picks, is a distribution and is on the evaluation page). Cash, the day's change, the fall from the high. The two circuit breakers, judged with the risk gate's own comparisons. The kill switch, put to the gate's own function: that is the switch as the dashboard sees it at that moment, which covers the switch file but not a `TRADING_HALT` set for the pipeline alone, so what the last finished run itself recorded about being halted is shown beside it. |
| 会议室 | The main page. Fourteen figures stand in a circle: seven macro analysts, the ticker analyst, the bull and the bear facing each other, the judge, the portfolio manager, and the risk officer and the trader, who are rules and are marked as rules. They have positions, not names, and are drawn by code; none is modelled on a real person. The latest session is played in the order it was recorded: whoever speaks is lit, the one spoken to is marked, the words appear in the middle, and everything else recorded with that line is listed below. While a run is going, new lines appear by themselves. |
| 持仓和订单 | Positions with their stops, orders waiting to fill, fills, and the last finished session's candidates, views, proposals and risk decisions. |
| 评估 | The last evaluation report as it was written: model configurations side by side, the scorecard, stated confidence against the hit rate, trades, where the account falls among random picks, model usage, and the lessons on record. The dashboard never runs the evaluation; `python -m evaluation` refreshes it. |
| 回放 | Any past session, played back the same way: pick a day and the run that counts for it is shown. |
| 运行状态 | Every run with its duration, model calls, cost, warnings and errors, and a link that replays that one run, a failed or replaced attempt included. |

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
| `PIPELINE_MACRO_MAX_AGE_DAYS` | `7` | The macro view is reused until it is this old |
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
├── scripts/               # daily_run.sh (one unattended run) and schedule.sh (launchd schedule, macOS)
├── tests/                 # pytest suite (no real model needed)
├── main.py                # CLI entry point (Rich terminal UI, preferences)
├── app.py                 # Streamlit web UI
├── requirements.txt       # Python dependencies
├── .env                   # API key (not committed)
├── .env.example           # Template for .env
├── pipeline/
│   ├── daily.py           # The fixed daily pipeline (paper trading) and its command line
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
│   └── store.py           # SQLite journal: runs, events, macro views, paper books, checkpoints, scores, lessons
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
│   └── static/            # The page: index.html, styles.css, js/ (meeting room, playback, charts, pages)
├── agents/
│   ├── ticker/            # Ticker analyst, bull, bear, judge, portfolio manager, reviewer
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

The dashboard page's own logic (playing a meeting back, following a run that is still going, number formats, seats) has tests in `tests/js`. `pytest` runs them with `node` when it is installed and skips them when it is not; `node --test tests/js/*.test.mjs` runs them alone.

## License

MIT
