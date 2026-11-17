Real samples recorded on 2026-11-16 for session 2026-11-13.

- `bars_*.csv`: daily bars as returned by `YFinanceProvider.daily_bars`.
- `raw_download.csv`: a two-symbol frame exactly as yfinance returns it.
- `fundamentals_AAPL.json`: `YFinanceProvider.fundamentals`.
- `fred_*.csv`: FRED's public CSV download.

Tests read these instead of calling the network.
