"""Minimal MCP test server — serves mock market data for testing the MCP integration."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mock-market-data")


@mcp.tool()
def get_rate(central_bank: str = "FED") -> str:
    """Get current interest rate for a central bank."""
    rates = {
        "FED": "5.25-5.50%",
        "ECB": "4.50%",
        "BOJ": "-0.10%",
        "BOE": "5.25%",
    }
    return f"{central_bank} current rate: {rates.get(central_bank.upper(), 'unknown')}"


@mcp.tool()
def get_price(ticker: str = "SPY") -> str:
    """Get latest price for a ticker symbol."""
    prices = {
        "SPY": "$542.30 (+0.4%)",
        "QQQ": "$467.15 (+0.6%)",
        "TLT": "$92.80 (-0.2%)",
        "GLD": "$218.50 (+0.1%)",
        "DXY": "104.2 (-0.3%)",
        "CL=F": "$78.50 (+1.2%)",
    }
    return f"{ticker}: {prices.get(ticker.upper(), 'no data')}"


@mcp.tool()
def get_economic_indicator(indicator: str = "CPI") -> str:
    """Get latest economic indicator value."""
    data = {
        "CPI": "CPI YoY: 3.2% (prev 3.4%), Core: 3.8%",
        "GDP": "GDP Q4: 3.3% annualized (prev 4.9%)",
        "UNEMPLOYMENT": "Unemployment: 3.7% (prev 3.8%)",
        "PCE": "PCE Core: 2.9% YoY (prev 3.2%)",
        "PMI": "Manufacturing PMI: 49.1 (contraction), Services PMI: 52.5",
    }
    return data.get(indicator.upper(), f"No data for {indicator}")


@mcp.tool()
def query(query: str) -> str:
    """General market query — returns relevant mock data based on keywords."""
    q = query.lower()
    results = []
    if any(w in q for w in ["rate", "fed", "monetary", "central bank"]):
        results.append("FED funds rate: 5.25-5.50%. Market pricing: 65% chance of cut by June.")
    if any(w in q for w in ["inflation", "cpi", "price"]):
        results.append("CPI trending down: 3.2% YoY. Core sticky at 3.8%. PCE improving.")
    if any(w in q for w in ["stock", "equity", "market", "s&p"]):
        results.append("S&P 500 at 5,420. P/E 21.5x. Earnings growth +8% YoY.")
    if any(w in q for w in ["bond", "yield", "treasury"]):
        results.append("10Y yield: 4.25%. 2Y: 4.60%. Curve inverted -35bps.")
    if any(w in q for w in ["oil", "commodity", "gold"]):
        results.append("WTI: $78.50. Gold: $2,180. Copper: +5% MTD.")
    if any(w in q for w in ["dollar", "currency", "forex", "usd"]):
        results.append("DXY: 104.2. EUR/USD: 1.085. USD/JPY: 150.3.")
    if not results:
        results.append("General market conditions: risk-on sentiment, VIX at 13.5.")
    return " | ".join(results)


if __name__ == "__main__":
    mcp.run(transport="stdio")
