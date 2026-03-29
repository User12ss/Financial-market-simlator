# Financial Market Simulation — Multi-Agent Framework

An AI-powered multi-agent system that simulates how financial markets react to macroeconomic, monetary, and fiscal policy changes. Built on the Anthropic Claude API.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Orchestrator                    │
│         (parses input, coordinates agents,       │
│          merges outputs, builds report)          │
└──────────┬──────────────────────┬───────────────┘
           │                      │
   ┌───────▼───────┐    ┌────────▼────────┐
   │  Phase 1:      │    │  Phase 2:        │
   │  Independent   │    │  Cross-Domain    │
   │  Agent Runs    │    │  Synthesis       │
   └───────┬───────┘    └────────┬────────┘
           │                      │
  ┌────────▼──────────────────────▼────────┐
  │  7 Specialized Agents                   │
  │  ┌──────────┐ ┌──────────┐ ┌────────┐ │
  │  │ Central  │ │  Fiscal  │ │ Equity │ │
  │  │  Bank    │ │  Policy  │ │ Market │ │
  │  └──────────┘ └──────────┘ └────────┘ │
  │  ┌──────────┐ ┌──────────┐ ┌────────┐ │
  │  │  Fixed   │ │  Forex   │ │Commod. │ │
  │  │  Income  │ │          │ │& Infl. │ │
  │  └──────────┘ └──────────┘ └────────┘ │
  │  ┌──────────┐                          │
  │  │Sentiment │                          │
  │  │& Behav.  │                          │
  │  └──────────┘                          │
  └────────────────────────────────────────┘
```

## Agents

| Agent | Domain | Key Outputs |
|-------|--------|-------------|
| Central Bank | Monetary policy, rates, QE/QT | Rate path, liquidity conditions |
| Fiscal Policy | Gov spending, taxes, deficits | Demand impact, debt trajectory |
| Equity Market | Stock sectors, valuations | Sector rotation, P/E shifts |
| Fixed Income | Bonds, yield curve, spreads | Yield moves, curve shape |
| Forex | Exchange rates, capital flows | Currency direction, carry trade |
| Commodities & Inflation | Prices, CPI/PPI | Inflation path, commodity moves |
| Sentiment & Behavioral | Psychology, risk appetite | Risk-on/off, positioning |

## Setup

```bash
# 1. Clone and enter the project
cd financial-market-sim

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your API key
cp .env.example .env
# Edit .env and add your Anthropic API key
```

## Usage

### CLI Mode
```bash
# Interactive mode
python main.py

# Single scenario
python main.py --scenario "The Fed cuts rates by 50bps unexpectedly"

# Compare two scenarios
python main.py --compare \
  --scenario-a "Fed raises rates 25bps" \
  --scenario-b "Fed raises rates 100bps"
```

### Web UI (Streamlit)
```bash
streamlit run app.py
```

### Python API
```python
from orchestrator import Orchestrator

orch = Orchestrator()
report = orch.run("What happens if the Fed raises rates by 100bps unexpectedly?")
print(report.to_markdown())
```

## Configuration

Edit `config.py` or set environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Your Anthropic API key (required) |
| `MODEL_NAME` | `claude-sonnet-4-20250514` | Claude model to use |
| `BASE_ECONOMY` | `US` | Default economy for simulations |
| `MAX_RETRIES` | `3` | API retry attempts |

## Example Scenarios

- *"The Fed raises interest rates by 100bps unexpectedly while inflation is at 6%"*
- *"Congress passes a $2 trillion infrastructure stimulus funded by new corporate taxes"*
- *"China devalues the yuan by 10% in response to trade tariffs"*
- *"The ECB begins quantitative tightening while the US cuts rates"*
- *"Oil prices spike to $150/barrel due to Middle East supply disruption"*

## License

MIT
