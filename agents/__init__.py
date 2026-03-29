from agents.base import BaseAgent
from agents.central_bank import CentralBankAgent
from agents.fiscal_policy import FiscalPolicyAgent
from agents.equity_market import EquityMarketAgent
from agents.fixed_income import FixedIncomeAgent
from agents.forex import ForexAgent
from agents.commodities import CommoditiesInflationAgent
from agents.sentiment import SentimentAgent

ALL_AGENTS: list[type[BaseAgent]] = [
    CentralBankAgent,
    FiscalPolicyAgent,
    EquityMarketAgent,
    FixedIncomeAgent,
    ForexAgent,
    CommoditiesInflationAgent,
    SentimentAgent,
]
