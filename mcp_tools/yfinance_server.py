"""MCP server exposing the project's data layer to outside MCP clients.

A thin wrapper: every tool builds a snapshot through `data.snapshot` and
returns the same text the simulator's own agents are shown. The simulator
does not use this server; it calls the data layer directly.

Run: python mcp_tools/yfinance_server.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from data.render import render_market, render_snapshot  # noqa: E402
from data.snapshot import SnapshotBuilder  # noqa: E402

logging.getLogger("mcp").setLevel(logging.WARNING)

mcp = FastMCP("market-data")
_builder = SnapshotBuilder()


@mcp.tool()
def get_market_data() -> str:
    """Current US market levels (equity indices, VIX, dollar, gold, oil, Treasury
    yields, sector ETFs) and macro series from FRED (policy rate, inflation,
    jobs, credit spread), as of the last completed trading session."""
    return render_market(_builder.build()) or "No market data was returned."


@mcp.tool()
def get_tickers(tickers: str) -> str:
    """Price, returns, technical indicators, fundamentals, next earnings date and
    recent headlines for one or more symbols, comma-separated (e.g. "AAPL,MSFT"),
    as of the last completed trading session."""
    snapshot = _builder.build(tickers.split(","), include_market=False, include_macro=False)
    return render_snapshot(snapshot) or "No data was returned for those symbols."


if __name__ == "__main__":
    mcp.run(transport="stdio")
