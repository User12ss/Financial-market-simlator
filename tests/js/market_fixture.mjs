// One run's market data as /api/market answers it, made up for the tests that read it, and three of the figures.

const point = (group, name, symbol, value, moves = {}) => ({ group, name, symbol, last_date: "2026-11-12", value, ...moves });
export const MARKET = {
  available: true, session: "2026-11-12", run_id: "run-1",
  market: [
    point("market", "S&P 500", "^GSPC", 7592.4, { change_1d_pct: -0.35, return_5d_pct: 3.25, return_1mo_pct: -1.64 }),
    point("market", "VIX", "^VIX", 18.7, { change_1d_pct: 6.92, return_5d_pct: 0, return_1mo_pct: null }),
    point("yields", "3-month Treasury yield", "^IRX", 4.13, { change_1d: 0.01, change_5d: 0.14, change_1mo: -0.04, change_1d_pct: 0.24 }),
    point("yields", "10-year Treasury yield", "^TNX", 5.1, { change_1d: 0.1, change_5d: 0, change_1mo: -0.09 }),
    point("yields", "30-year Treasury yield", "^TYX", 5.43, {}),
    point("sectors", "Technology", "XLK", 250, { change_1d_pct: 0.1, return_5d_pct: 1, return_1mo_pct: -8.75 }),
    point("sectors", "Healthcare", "XLV", 160, { change_1d_pct: 0.2, return_5d_pct: 2, return_1mo_pct: 7.97 }),
    point("sectors", "A fund with no name here", "XYZ", 10, { return_1mo_pct: null }),
    point("sectors", "Energy", "XLE", 90, { change_1d_pct: -0.3, return_5d_pct: -1, return_1mo_pct: -4.88 }),
  ],
  macro: [
    { series_id: "FEDFUNDS", name: "Effective federal funds rate", unit: "%", frequency: "monthly", latest_date: "2026-10-01", latest_value: 3.88, previous_value: 3.75, yoy_change_pct: null },
    { series_id: "CPIAUCSL", name: "Consumer price index", unit: "index", frequency: "monthly", latest_date: "2026-09-01", latest_value: 331.2, previous_value: 330.1, yoy_change_pct: 3.01 },
    { series_id: "PAYEMS", name: "Nonfarm payrolls", unit: "thousand jobs", frequency: "monthly", latest_date: "2026-10-01", latest_value: 160250, previous_value: 160300, yoy_change_pct: null },
    { series_id: "T10Y2Y", name: "10-year minus 2-year Treasury spread", unit: "pp", frequency: "daily", latest_date: "2026-11-11", latest_value: 0.24, previous_value: 0.25, yoy_change_pct: null },
    { series_id: "NEWONE", name: "A series added later", unit: "USD per barrel", frequency: "daily", latest_date: "2026-11-11", latest_value: 61.5, previous_value: null, yoy_change_pct: null },
  ],
  tickers: {
    TMO: {
      name: "Thermo Fisher", sector: "Healthcare",
      price: { close: 100, change_1d_pct: -2.55, return_5d_pct: 1.5, return_1mo_pct: 0, return_3mo_pct: null, high_52w: 120, low_52w: 80 },
      technicals: { sma20: 98, sma50: null, sma200: 90, rsi14: 44.44, bollinger_lower: 92, bollinger_upper: 108, atr14: 3, atr14_pct: 3 },
      bars: { dates: ["2026-11-10", "2026-11-11", "2026-11-12"], open: [96, 104, 102], high: [106, 110, 104], low: [94, 100, 90], close: [104, 102, 100], volume: [50, 100, 25] },
      after: { through: "2026-11-16", dates: ["2026-11-13", "2026-11-16"], open: [100, 103], high: [104, 106], low: [99, 101], close: [103, 105], volume: [10, 20] },
    },
    ABBV: {
      name: "AbbVie", sector: "Healthcare", price: { close: 294.45, change_1d_pct: 0 }, technicals: null,
      bars: { dates: ["2026-11-12"], open: [294], high: [296], low: [293], close: [294.45], volume: [7] }, after: null,
    },
    GONE: { name: "", sector: "", price: null, technicals: null, bars: { dates: [], open: [], high: [], low: [], close: [], volume: [] }, after: null },
  },
};
export const PERSONAS = new Map([
  ["fixed_income", { id: "fixed_income", title: "债市分析师", craft: ["收益率曲线", "期限利差", "信用利差"], reads: ["^IRX", "^TNX", "T10Y2Y"] }],
  ["ticker_analyst", { id: "ticker_analyst", title: "标的分析师", craft: ["价格走势", "基本面", "技术指标"], reads: [] }],
  ["equity_market", { id: "equity_market", title: "股市分析师", craft: ["板块轮动", "估值", "风险偏好"], reads: ["^GSPC", "^VIX", "sectors"] }],
]);
