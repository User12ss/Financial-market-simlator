"""MCP server providing real financial market data via yfinance.

Exposes tools for:
- Stock/ETF prices and changes
- Key market indicators (VIX, yields, DXY)
- Technical indicators (SMA, RSI, MACD)
- Economic-relevant ETF data (TLT, GLD, USO, etc.)
- Sector performance and fundamentals

Run: python mcp_tools/yfinance_server.py
"""

import logging
import yfinance as yf
import pandas as pd
from mcp.server.fastmcp import FastMCP

logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server").setLevel(logging.WARNING)

mcp = FastMCP("yfinance-market-data")


@mcp.tool()
def get_price(ticker: str) -> str:
    """Get current price, daily change, and 52-week range for a ticker (e.g. SPY, AAPL, GLD)."""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        price = info.get("regularMarketPrice") or info.get("previousClose")
        prev = info.get("previousClose")
        high52 = info.get("fiftyTwoWeekHigh")
        low52 = info.get("fiftyTwoWeekLow")
        name = info.get("shortName", ticker)

        if price and prev:
            change = price - prev
            pct = (change / prev) * 100
            result = f"{name} ({ticker}): ${price:.2f} ({change:+.2f}, {pct:+.2f}%)"
        else:
            result = f"{ticker}: ${price}"

        if high52 and low52:
            result += f" | 52w range: ${low52:.2f}-${high52:.2f}"

        return result
    except Exception as e:
        return f"Error fetching {ticker}: {e}"


@mcp.tool()
def get_market_overview() -> str:
    """Get snapshot of major market indicators: S&P500, Nasdaq, VIX, 10Y yield, DXY, Gold, Oil."""
    tickers = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "VIX": "^VIX",
        "10Y Treasury": "^TNX",
        "US Dollar (DXY)": "DX-Y.NYB",
        "Gold": "GC=F",
        "Crude Oil": "CL=F",
    }

    results = []
    data = yf.download(list(tickers.values()), period="2d", progress=False)

    for name, symbol in tickers.items():
        try:
            if "Close" in data.columns and symbol in data["Close"].columns:
                closes = data["Close"][symbol].dropna()
                if len(closes) >= 2:
                    current = closes.iloc[-1]
                    prev = closes.iloc[-2]
                    pct = ((current - prev) / prev) * 100
                    results.append(f"{name}: {current:.2f} ({pct:+.2f}%)")
                elif len(closes) == 1:
                    results.append(f"{name}: {closes.iloc[-1]:.2f}")
        except Exception:
            pass

    return "MARKET OVERVIEW:\n" + "\n".join(results) if results else "Unable to fetch market data"


@mcp.tool()
def get_historical(ticker: str, period: str) -> str:
    """Get historical price summary. Ticker examples: SPY, QQQ, TLT. Period: 1d, 5d, 1mo, 3mo, 6mo, 1y."""
    try:
        data = yf.download(ticker, period=period, progress=False)
        if data.empty:
            return f"No data for {ticker} over {period}"

        first = data["Close"].iloc[0]
        last = data["Close"].iloc[-1]
        high = data["High"].max()
        low = data["Low"].min()

        if hasattr(first, 'item'):
            first = first.item()
        if hasattr(last, 'item'):
            last = last.item()
        if hasattr(high, 'item'):
            high = high.item()
        if hasattr(low, 'item'):
            low = low.item()

        total_return = ((last - first) / first) * 100

        result = (
            f"{ticker} ({period}): "
            f"Start ${first:.2f} -> End ${last:.2f} ({total_return:+.2f}%) | "
            f"High ${high:.2f} | Low ${low:.2f}"
        )
        return result
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_technical_indicators(ticker: str) -> str:
    """Get technical indicators for a ticker: SMA(20,50), RSI(14), MACD, Bollinger Bands. Uses 3 months of data."""
    try:
        data = yf.download(ticker, period="3mo", progress=False)
        if data.empty or len(data) < 20:
            return f"Insufficient data for {ticker}"

        close = data["Close"].squeeze()

        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else None

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = (100 - (100 / (1 + rs))).iloc[-1]

        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd = (ema12 - ema26).iloc[-1]
        signal = (ema12 - ema26).ewm(span=9).mean().iloc[-1]

        # Bollinger Bands
        bb_std = close.rolling(20).std().iloc[-1]
        bb_upper = sma20 + 2 * bb_std
        bb_lower = sma20 - 2 * bb_std

        current = close.iloc[-1]

        lines = [
            f"{ticker} Technical Indicators:",
            f"  Price: ${current:.2f}",
            f"  SMA(20): ${sma20:.2f} ({'above' if current > sma20 else 'below'})",
        ]
        if sma50:
            lines.append(f"  SMA(50): ${sma50:.2f} ({'above' if current > sma50 else 'below'})")

        lines.extend([
            f"  RSI(14): {rsi:.1f} ({'overbought' if rsi > 70 else 'oversold' if rsi < 30 else 'neutral'})",
            f"  MACD: {macd:.3f} | Signal: {signal:.3f} ({'bullish' if macd > signal else 'bearish'})",
            f"  Bollinger: ${bb_lower:.2f} / ${sma20:.2f} / ${bb_upper:.2f}",
        ])

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_yield_curve() -> str:
    """Get current US Treasury yield curve (3M, 2Y, 5Y, 10Y, 30Y) and key spreads."""
    tickers = {
        "3M": "^IRX",
        "5Y": "^FVX",
        "10Y": "^TNX",
        "30Y": "^TYX",
    }

    yields_data = {}
    for label, symbol in tickers.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if not hist.empty:
                yields_data[label] = hist["Close"].iloc[-1]
        except Exception:
            pass

    if not yields_data:
        return "Unable to fetch yield curve data"

    lines = ["US TREASURY YIELD CURVE:"]
    for label, val in yields_data.items():
        lines.append(f"  {label}: {val:.3f}%")

    if "10Y" in yields_data and "3M" in yields_data:
        spread = yields_data["10Y"] - yields_data["3M"]
        lines.append(f"  3M-10Y Spread: {spread:+.3f}% ({'inverted' if spread < 0 else 'normal'})")

    return "\n".join(lines)


@mcp.tool()
def get_sector_performance(period: str) -> str:
    """Get S&P 500 sector ETF performance ranked best to worst. Period: 1d, 5d, 1mo, 3mo, 6mo, 1y."""
    sectors = {
        "Technology": "XLK",
        "Healthcare": "XLV",
        "Financials": "XLF",
        "Energy": "XLE",
        "Consumer Disc": "XLY",
        "Consumer Staples": "XLP",
        "Industrials": "XLI",
        "Materials": "XLB",
        "Utilities": "XLU",
        "Real Estate": "XLRE",
        "Communications": "XLC",
    }

    data = yf.download(list(sectors.values()), period=period, progress=False)
    if data.empty:
        return "Unable to fetch sector data"

    performances = []
    for name, sym in sectors.items():
        try:
            closes = data["Close"][sym].dropna()
            if len(closes) >= 2:
                ret = ((closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0]) * 100
                performances.append((name, sym, ret))
        except Exception:
            pass

    performances.sort(key=lambda x: x[2], reverse=True)

    lines = [f"SECTOR PERFORMANCE ({period}):"]
    for name, sym, ret in performances:
        lines.append(f"  {name} ({sym}): {ret:+.2f}%")

    return "\n".join(lines)


@mcp.tool()
def get_fundamentals(ticker: str) -> str:
    """Get key fundamentals: P/E, market cap, revenue, margins, dividend yield for a stock ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.info

        fields = {
            "Market Cap": ("marketCap", lambda x: f"${x/1e9:.1f}B"),
            "P/E (Trailing)": ("trailingPE", lambda x: f"{x:.1f}"),
            "P/E (Forward)": ("forwardPE", lambda x: f"{x:.1f}"),
            "EPS": ("trailingEps", lambda x: f"${x:.2f}"),
            "Revenue": ("totalRevenue", lambda x: f"${x/1e9:.1f}B"),
            "Profit Margin": ("profitMargins", lambda x: f"{x*100:.1f}%"),
            "Dividend Yield": ("dividendYield", lambda x: f"{x*100:.2f}%" if x else "N/A"),
            "Beta": ("beta", lambda x: f"{x:.2f}"),
        }

        lines = [f"{info.get('shortName', ticker)} ({ticker}) Fundamentals:"]
        for label, (key, fmt) in fields.items():
            val = info.get(key)
            if val is not None:
                lines.append(f"  {label}: {fmt(val)}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching fundamentals for {ticker}: {e}"


@mcp.tool()
def query(query: str) -> str:
    """General financial query — fetches relevant real-time data based on keywords in the query.

    Handles queries about: rates, inflation, stocks, bonds, commodities, currencies.
    """
    q = query.lower()
    results = []

    if any(w in q for w in ["rate", "fed", "monetary", "central bank", "interest"]):
        try:
            tnx = yf.Ticker("^TNX").history(period="5d")
            irx = yf.Ticker("^IRX").history(period="5d")
            if not tnx.empty:
                results.append(f"10Y Treasury Yield: {tnx['Close'].iloc[-1]:.3f}%")
            if not irx.empty:
                results.append(f"3M Treasury Yield: {irx['Close'].iloc[-1]:.3f}%")
            tlt = yf.Ticker("TLT").history(period="5d")
            if not tlt.empty:
                ret = ((tlt["Close"].iloc[-1] - tlt["Close"].iloc[0]) / tlt["Close"].iloc[0]) * 100
                results.append(f"TLT (20Y+ bonds) 5d: {ret:+.2f}%")
        except Exception:
            pass

    if any(w in q for w in ["inflation", "cpi", "price level", "commodity"]):
        try:
            tip = yf.Ticker("TIP").history(period="1mo")
            gld = yf.Ticker("GLD").history(period="1mo")
            uso = yf.Ticker("USO").history(period="1mo")
            if not tip.empty:
                ret = ((tip["Close"].iloc[-1] - tip["Close"].iloc[0]) / tip["Close"].iloc[0]) * 100
                results.append(f"TIP (inflation-protected) 1mo: {ret:+.2f}%")
            if not gld.empty:
                ret = ((gld["Close"].iloc[-1] - gld["Close"].iloc[0]) / gld["Close"].iloc[0]) * 100
                results.append(f"Gold (GLD) 1mo: {ret:+.2f}%")
            if not uso.empty:
                ret = ((uso["Close"].iloc[-1] - uso["Close"].iloc[0]) / uso["Close"].iloc[0]) * 100
                results.append(f"Oil (USO) 1mo: {ret:+.2f}%")
        except Exception:
            pass

    if any(w in q for w in ["stock", "equity", "market", "s&p", "nasdaq"]):
        try:
            spy = yf.Ticker("SPY").history(period="5d")
            qqq = yf.Ticker("QQQ").history(period="5d")
            vix = yf.Ticker("^VIX").history(period="5d")
            if not spy.empty:
                ret = ((spy["Close"].iloc[-1] - spy["Close"].iloc[0]) / spy["Close"].iloc[0]) * 100
                results.append(f"S&P 500 (SPY) 5d: {ret:+.2f}%, price ${spy['Close'].iloc[-1]:.2f}")
            if not qqq.empty:
                ret = ((qqq["Close"].iloc[-1] - qqq["Close"].iloc[0]) / qqq["Close"].iloc[0]) * 100
                results.append(f"Nasdaq (QQQ) 5d: {ret:+.2f}%")
            if not vix.empty:
                results.append(f"VIX: {vix['Close'].iloc[-1]:.2f}")
        except Exception:
            pass

    if any(w in q for w in ["bond", "yield", "treasury", "fixed income", "credit"]):
        try:
            tnx = yf.Ticker("^TNX").history(period="5d")
            tyx = yf.Ticker("^TYX").history(period="5d")
            hyg = yf.Ticker("HYG").history(period="1mo")
            if not tnx.empty:
                results.append(f"10Y Yield: {tnx['Close'].iloc[-1]:.3f}%")
            if not tyx.empty:
                results.append(f"30Y Yield: {tyx['Close'].iloc[-1]:.3f}%")
            if not hyg.empty:
                ret = ((hyg["Close"].iloc[-1] - hyg["Close"].iloc[0]) / hyg["Close"].iloc[0]) * 100
                results.append(f"High Yield (HYG) 1mo: {ret:+.2f}%")
        except Exception:
            pass

    if any(w in q for w in ["dollar", "currency", "forex", "usd", "yen", "euro"]):
        try:
            dxy = yf.Ticker("DX-Y.NYB").history(period="5d")
            eurusd = yf.Ticker("EURUSD=X").history(period="5d")
            usdjpy = yf.Ticker("JPY=X").history(period="5d")
            if not dxy.empty:
                results.append(f"DXY (USD Index): {dxy['Close'].iloc[-1]:.2f}")
            if not eurusd.empty:
                results.append(f"EUR/USD: {eurusd['Close'].iloc[-1]:.4f}")
            if not usdjpy.empty:
                results.append(f"USD/JPY: {usdjpy['Close'].iloc[-1]:.2f}")
        except Exception:
            pass

    if any(w in q for w in ["oil", "gold", "commodity", "copper", "energy"]):
        try:
            cl = yf.Ticker("CL=F").history(period="5d")
            gc = yf.Ticker("GC=F").history(period="5d")
            if not cl.empty:
                results.append(f"Crude Oil (WTI): ${cl['Close'].iloc[-1]:.2f}")
            if not gc.empty:
                results.append(f"Gold: ${gc['Close'].iloc[-1]:.2f}")
        except Exception:
            pass

    if not results:
        try:
            spy = yf.Ticker("SPY").history(period="2d")
            vix = yf.Ticker("^VIX").history(period="2d")
            if not spy.empty:
                results.append(f"S&P 500: ${spy['Close'].iloc[-1]:.2f}")
            if not vix.empty:
                results.append(f"VIX: {vix['Close'].iloc[-1]:.2f}")
        except Exception:
            results.append("Unable to fetch market data")

    return " | ".join(results)


if __name__ == "__main__":
    mcp.run(transport="stdio")
