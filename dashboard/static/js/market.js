// What the market screens show, worked out from the data snapshot a run
// recorded (/api/market) and from the lines of its record played so far.
// Every price, level and figure here is one the snapshot or the record holds;
// the only arithmetic is where a recorded number goes on a plot, and plain
// differences between recorded numbers (a move since the session's close, a
// place inside a recorded range). No page in sight, so it can be tested alone.

import { num, pct, signedPct, usd, word } from "./dom.js";
import { reached, tickerOf } from "./decisions.js";
import { t } from "./i18n.js";

export const BARS_ON_SCREEN = 60;           // about three months of sessions
const SPARK = 30;

// The page's own names for the series every snapshot records. A series without one keeps its recorded name.
const NAMES = {
  "^GSPC": t("标普 500"), "^IXIC": t("纳斯达克"), "^VIX": t("VIX 波动率"), "DX-Y.NYB": t("美元指数"), "GC=F": t("黄金期货"),
  "CL=F": t("WTI 原油期货"), "^IRX": t("3 个月国债"), "^FVX": t("5 年期国债"), "^TNX": t("10 年期国债"), "^TYX": t("30 年期国债"),
  XLK: t("科技"), XLV: t("医疗"), XLF: t("金融"), XLE: t("能源"), XLY: t("可选消费"), XLP: t("必需消费"), XLI: t("工业"), XLB: t("材料"),
  XLU: t("公用事业"), XLRE: t("房地产"), XLC: t("通信服务"),
  FEDFUNDS: t("联邦基金利率"), CPIAUCSL: "CPI", CPILFESL: t("核心 CPI"), UNRATE: t("失业率"), PAYEMS: t("非农就业"),
  DGS2: t("2 年期国债"), DGS10: t("10 年期国债"), T10Y2Y: t("10 年减 2 年利差"), BAMLH0A0HYM2: t("高收益债利差"),
  DCOILWTICO: t("WTI 原油现货"),
};
const CURVE = [["^IRX", "3M"], ["^FVX", "5Y"], ["^TNX", "10Y"], ["^TYX", "30Y"]];

const given = (value) => typeof value === "number" && Number.isFinite(value);
const leanOf = (value) => (!given(value) || value === 0 ? "flat" : value > 0 ? "up" : "down");
/** A move in percentage points, signed: how a change in a yield or a spread is quoted. */
const points = (value) => (given(value) ? t`${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)} 点` : "—");

/**
 * What a screen is about at one line of the record.
 *  - a line about one ticker: that ticker's chart;
 *  - the portfolio manager's list: the tickers on it, side by side;
 *  - a macro analyst speaking: the recorded series of that analyst's field;
 *  - anything else (nothing said yet, the pipeline's own lines): the market as a whole.
 */
export function focusOf(event, personas) {
  if (!event) return { kind: "overview" };
  if (event.kind === "proposals" || event.kind === "checkin_decisions") {
    const tickers = [];
    const said = event.payload || {};
    for (const item of (event.kind === "proposals" ? said.proposals : said.decisions) || []) {
      const ticker = tickerOf(item.ticker);
      if (ticker && !tickers.includes(ticker)) tickers.push(ticker);
    }
    return { kind: "list", tickers };
  }
  if (event.ticker) return { kind: "ticker", ticker: event.ticker };
  const persona = personas.get(event.speaker);
  if (persona && persona.reads && persona.reads.length) return { kind: "desk", persona: persona.id };
  return { kind: "overview" };
}

/** The last `count` of a ticker's recorded bars, one object per session. */
export function lastBars(bars, count = BARS_ON_SCREEN) {
  const from = Math.max(0, bars.dates.length - count);
  return bars.dates.slice(from).map((date, at) => ({
    date, open: bars.open[from + at], high: bars.high[from + at], low: bars.low[from + at],
    close: bars.close[from + at], volume: bars.volume[from + at],
  }));
}

const BANDS = ["5", "25", "50", "75", "95"];

/**
 * The quant engineer's forecast for a ticker, once its line of the record has been reached: where the recorded
 * percentiles of the simulated paths lie after each horizon. The last one said by line `upTo` is the one that stands.
 */
export function fanOf(ticker, events, upTo) {
  for (let index = Math.min(upTo, events.length - 1); index >= 0; index -= 1) {
    const event = events[index];
    if (event.kind !== "forecast" || event.ticker !== ticker) continue;
    const said = event.payload || {};
    const bands = (said.horizons || [])
      .filter((item) => given(item.sessions) && item.quantiles && BANDS.every((band) => given(item.quantiles[band])))
      .map((item) => ({ sessions: item.sessions, ...Object.fromEntries(BANDS.map((band) => [band, item.quantiles[band]])) }))
      .sort((a, b) => a.sessions - b.sessions);
    return bands.length ? { index, bands } : null;
  }
  return null;
}

/** One horizon of a forecast in words: where half the simulated paths end, and nine in ten. */
export function fanText(band) {
  return t`${band.sessions} 个交易日后：一半的模拟路径落在 ${usd(band["25"])} 到 ${usd(band["75"])}，`
    + t`九成落在 ${usd(band["5"])} 到 ${usd(band["95"])}`;
}

/**
 * Where candles go on a plot. `before` are the bars up to the session,
 * `after` the ones recorded later (empty when they are not to be shown), and
 * `levels` are prices to mark (a stop, a limit). A level is kept on the price
 * scale only when it is near the candles: one far away would squash them, so
 * it is reported as lying off the plot, above or below.
 * `fan` is a recorded forecast (see fanOf): it is given room to the right of the session, one place for each
 * session ahead, which are the same places the bars recorded afterwards go into.
 * Returns the candles with their places, the levels, the fan and the scale used.
 */
export function plot(before, after, levels, { width, height, volume = 0.16, gap = 0.28, fan = null }) {
  const all = [...before, ...after];
  if (all.length === 0) return { candles: [], levels: [], low: null, high: null, cut: null, step: 0, bottom: 0, fan: null };
  let low = Math.min(...all.map((bar) => bar.low)), high = Math.max(...all.map((bar) => bar.high));
  const reach = (high - low || Math.abs(high) * 0.02 || 1) * 0.6;
  const placed = levels.map((level) => ({
    ...level, off: level.price > high + reach ? "above" : level.price < low - reach ? "below" : null,
  }));
  for (const level of placed) {
    if (!level.off) {
      low = Math.min(low, level.price);
      high = Math.max(high, level.price);
    }
  }
  const bands = fan && before.length ? fan.bands : [];
  for (const band of bands) {
    low = Math.min(low, band["5"]);
    high = Math.max(high, band["95"]);
  }
  const room = (high - low || Math.abs(high) * 0.02 || 1) * 0.06;         // a little air above and below
  low -= room;
  high += room;
  const top = 0, bottom = height * (1 - volume) - 4;
  const y = (price) => top + ((high - price) / (high - low)) * (bottom - top);
  const ahead = bands.length ? bands[bands.length - 1].sessions : 0;
  const step = width / (before.length + Math.max(after.length, ahead));
  const most = Math.max(...all.map((bar) => bar.volume || 0)) || 1;
  const candles = all.map((bar, at) => ({
    ...bar, after: at >= before.length, up: bar.close >= bar.open,
    x: step * (at + 0.5), w: Math.max(1, step * (1 - gap)),
    yHigh: y(bar.high), yLow: y(bar.low), yOpen: y(bar.open), yClose: y(bar.close),
    yVolume: height - ((bar.volume || 0) / most) * (height * volume),
  }));
  const today = candles[before.length - 1];
  return {
    candles, low, high, step, y, bottom,
    levels: placed.map((level) => ({ ...level, y: level.off ? null : y(level.price) })),
    cut: after.length ? step * before.length : null,          // where the session ends and what came after begins
    // The fan opens from the session's close: each horizon sits as many places to the right as it is sessions ahead.
    fan: bands.length ? {
      x: today.x, y: today.yClose,
      points: bands.map((band) => ({
        ...band, x: step * (before.length - 0.5 + band.sessions),
        ...Object.fromEntries(BANDS.map((name) => [`y${name}`, y(band[name])])),
      })),
    } : null,
  };
}

/**
 * The prices a ticker's line of the decision trail has put on record by line `upTo`: stops and limits, and, at
 * a check-in of the desk, where the ticker was trading when the news desk last fetched its price that day.
 */
export function levelsOf(row, events, upTo) {
  const found = [];
  if (!row) return found;
  if (reached(row.view, upTo)) {
    const stop = events[row.view.index].payload.proposed_stop;
    if (given(stop)) found.push({ kind: "stop", by: "analyst", price: stop, index: row.view.index });
  }
  const last = Math.min(upTo, events.length - 1);
  for (let index = 0; index <= last; index += 1) {
    const event = events[index];
    const price = event.kind === "news" && event.ticker === row.ticker ? (event.payload || {}).trading : null;
    if (price && given(price.last)) {
      found.push({ kind: "now", by: "quote", price: price.last, change: price.change_pct, index });
      break;
    }
  }
  if (reached(row.risk, upTo) && row.risk.verdict !== "rejected") {
    const request = events[row.risk.index].payload.request || {};
    if (given(request.protective_stop)) found.push({ kind: "stop", by: "order", price: request.protective_stop, index: row.risk.index });
    if (given(request.limit_price)) found.push({ kind: "limit", by: "order", price: request.limit_price, index: row.risk.index });
  }
  return found;
}

/** What a level is called on the screen. */
export function levelText(level) {
  const where = level.off === "above" ? t("（在图上方）") : level.off === "below" ? t("（在图下方）") : "";
  if (level.kind === "now") {
    const change = given(level.change) ? ` ${level.change > 0 ? "▲ +" : level.change < 0 ? "▼ −" : ""}${Math.abs(level.change).toFixed(2)}%` : "";
    return t`当天盘中报价 ${usd(level.price)}${change}${where}`;
  }
  const name = level.kind === "limit" ? t("限价") : level.by === "analyst" ? t("分析师建议止损") : t("订单止损");
  return `${name} ${usd(level.price)}${where}`;
}

/** A ticker's recorded figures as short readouts: its price, its recent moves and how much it swings. */
export function readouts(item) {
  const price = item.price || {}, tech = item.technicals || {};
  const found = [
    { key: "close", label: t("收盘"), text: usd(price.close), lean: "flat" },
    { key: "d1", label: t("1 日"), text: signedPct(price.change_1d_pct), lean: leanOf(price.change_1d_pct) },
    { key: "d5", label: t("5 日"), text: signedPct(price.return_5d_pct), lean: leanOf(price.return_5d_pct) },
    { key: "m1", label: t("1 月"), text: signedPct(price.return_1mo_pct), lean: leanOf(price.return_1mo_pct) },
    { key: "m3", label: t("3 月"), text: signedPct(price.return_3mo_pct), lean: leanOf(price.return_3mo_pct) },
    { key: "atr", label: t("日均波幅 ATR"), text: given(tech.atr14_pct) ? pct(tech.atr14_pct) : "—", lean: "flat" },
    { key: "rsi", label: "RSI 14", text: num(tech.rsi14, 1), lean: "flat" },
  ];
  const place = rangePlace(price);
  if (place !== null) found.push({ key: "range", label: t("52 周区间位置"), text: `${Math.round(place * 100)}%`, lean: "flat" });
  return found;
}

/** Where the close sits between the recorded 52-week low (0) and high (1). */
export function rangePlace(price) {
  if (!price || !given(price.close) || !given(price.high_52w) || !given(price.low_52w) || price.high_52w <= price.low_52w) return null;
  return Math.min(1, Math.max(0, (price.close - price.low_52w) / (price.high_52w - price.low_52w)));
}

/** The recorded levels drawn at the right edge of a chart: averages, the Bollinger band, a day's usual swing. */
export function gauges(item) {
  const price = item.price || {}, tech = item.technicals || {};
  const lines = [["sma20", t("20 日均线")], ["sma50", t("50 日均线")], ["sma200", t("200 日均线")]]
    .filter(([key]) => given(tech[key])).map(([key, label]) => ({ key, label, price: tech[key] }));
  return {
    lines,
    band: given(tech.bollinger_lower) && given(tech.bollinger_upper) ? { low: tech.bollinger_lower, high: tech.bollinger_upper } : null,
    swing: given(price.close) && given(tech.atr14) ? { low: price.close - tech.atr14, high: price.close + tech.atr14 } : null,
  };
}

/** How a ticker has moved since the session, from the bars later sessions recorded. Null when there are none. */
export function sinceThen(item) {
  const after = item.after, bars = item.bars;
  if (!after || after.close.length === 0 || bars.close.length === 0) return null;
  const from = bars.close[bars.close.length - 1], to = after.close[after.close.length - 1];
  const change = from ? (to / from - 1) * 100 : null;
  return { days: after.close.length, through: after.through, from, to, change_pct: change, lean: leanOf(change) };
}

export function sinceText(since) {
  return t`之后 ${since.days} 个交易日（到 ${since.through}）：${usd(since.from)} → ${usd(since.to)}，${signedPct(since.change_pct)}`;
}

/** One recorded series as a reading: its name, its value, and its recent moves in the units they are quoted in. */
function reading(point) {
  if ("series_id" in point) {                                     // a macro series: latest, previous, year over year
    const value = point.unit === "%" || point.unit === "pp" ? `${point.latest_value.toFixed(2)}${point.unit === "%" ? "%" : t(" 点")}`
      : point.unit === "thousand jobs" ? t`${num(point.latest_value)} 千人` : num(point.latest_value, 2);
    const moves = [];
    if (given(point.previous_value)) {
      const move = point.latest_value - point.previous_value;
      moves.push({
        label: t("较上期"), lean: leanOf(move),
        text: point.unit === "thousand jobs" ? t`${move > 0 ? "+" : move < 0 ? "−" : ""}${num(Math.abs(move))} 千人`
          : point.unit === "index" ? (move > 0 ? "+" : move < 0 ? "−" : "") + num(Math.abs(move), 2) : points(move),
      });
    }
    if (given(point.yoy_change_pct)) moves.push({ label: t("同比"), text: signedPct(point.yoy_change_pct), lean: leanOf(point.yoy_change_pct) });
    return { id: point.series_id, name: NAMES[point.series_id] || point.name, value, as_of: point.latest_date, moves, trend: null };
  }
  const yields = point.group === "yields";                        // a yield's moves are quoted in points, not percent
  const move = (label, percent, own) => ({
    label, text: yields ? points(own) : signedPct(percent), lean: leanOf(yields ? own : percent),
  });
  return {
    id: point.symbol, name: NAMES[point.symbol] || point.name, as_of: point.last_date,
    value: yields ? `${point.value.toFixed(2)}%` : num(point.value, 2),
    moves: [move(t("1 日"), point.change_1d_pct, point.change_1d), move(t("5 日"), point.return_5d_pct, point.change_5d),
      move(t("1 月"), point.return_1mo_pct, point.change_1mo)],
    trend: trendOf(point),
  };
}

// How many sessions back the snapshot measures its "5 日" and "1 月" changes from (data/snapshot.py).
const WEEK = 5, MONTH = 21;

/**
 * Where a market series has been lately, oldest first, as far as the record says.
 *  - "daily": its recorded daily closes (the index and the sector funds, since snapshots began keeping them);
 *  - "marks": no closes on record, so the four values the snapshot does give: the session's own, and that
 *    value less its recorded change over one session, five and twenty-one. Nothing between them is known,
 *    and nothing is drawn as if it were.
 * `back` is how many sessions before the snapshot's session a value stood. Null with fewer than two values.
 */
export function trendOf(point) {
  if (!given(point.value)) return null;
  if (Array.isArray(point.closes) && point.closes.filter(given).length >= 2) {
    const closes = point.closes.filter(given);
    return { kind: "daily", points: closes.map((value, at) => ({ back: closes.length - 1 - at, value })) };
  }
  const marks = [[MONTH, point.change_1mo], [WEEK, point.change_5d], [1, point.change_1d]]
    .filter(([, change]) => given(change)).map(([back, change]) => ({ back, value: point.value - change }));
  return marks.length ? { kind: "marks", points: [...marks, { back: 0, value: point.value }] } : null;
}

/**
 * A trend on a small plot: across by how long ago, up by value. Returns the line, the same line closed along
 * the bottom (for a wash under it), where each value sits, and which way it went from its first value to its last.
 */
export function trendPlot(trend, width, height, pad = 2.5) {
  const values = trend.points.map((point) => point.value);
  const low = Math.min(...values), high = Math.max(...values), spread = high - low || 1;
  const far = Math.max(...trend.points.map((point) => point.back)) || 1;
  const dots = trend.points.map((point) => ({
    x: pad + (1 - point.back / far) * (width - 2 * pad), y: pad + ((high - point.value) / spread) * (height - 2 * pad),
    back: point.back, value: point.value,
  }));
  const line = dots.map((dot, at) => `${at ? "L" : "M"}${dot.x.toFixed(1)},${dot.y.toFixed(1)}`).join(" ");
  const first = dots[0], last = dots[dots.length - 1];
  return {
    line, dots, area: `${line} L${last.x.toFixed(1)},${height} L${first.x.toFixed(1)},${height} Z`,
    lean: leanOf(last.value - first.value),
  };
}

/**
 * The recorded series sorted under a field, in the order given. "sectors"
 * stands for every sector fund, strongest month first. A series the snapshot
 * does not hold is left out.
 */
export function readings(reads, market) {
  const found = [];
  for (const id of reads) {
    if (id === "sectors") {
      found.push(...sectors(market).map(reading));
      continue;
    }
    const point = market.market.find((item) => item.symbol === id) || market.macro.find((item) => item.series_id === id);
    if (point) found.push(reading(point));
  }
  return found;
}

/** The sector funds, strongest month first; those with no month on record last. */
export function sectors(market) {
  const rank = (point) => (given(point.return_1mo_pct) ? point.return_1mo_pct : -Infinity);
  return market.market.filter((point) => point.group === "sectors").sort((a, b) => rank(b) - rank(a));
}

/** The recorded Treasury yields in order of maturity, for a curve. */
export function yieldCurve(market) {
  return CURVE.map(([symbol, label]) => [market.market.find((point) => point.symbol === symbol), label])
    .filter(([point]) => point).map(([point, label]) => ({ label, value: point.value, text: `${point.value.toFixed(2)}%` }));
}

/** The market as a whole: the index, volatility, dollar and commodity series the snapshot records. */
export function headline(market) {
  return market.market.filter((point) => point.group === "market").map(reading);
}

/** A line through values on a small plot, as an SVG path. Null when there are fewer than two. */
export function sparkPath(values, width, height, pad = 1.5) {
  const kept = values.filter(given);
  if (kept.length < 2) return null;
  const low = Math.min(...kept), high = Math.max(...kept), spread = high - low || 1;
  return kept.map((value, at) => {
    const x = (at / (kept.length - 1)) * width;
    const y = pad + ((high - value) / spread) * (height - 2 * pad);
    return `${at ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

/** The latest closes of a ticker, for a small line beside its name. */
export const sparkOf = (item) => item.bars.close.slice(-SPARK);

/** The tape along the top: every ticker of the snapshot, then the market series. */
export function tape(market) {
  const tickers = Object.entries(market.tickers).filter(([, item]) => item.price).sort(([a], [b]) => a.localeCompare(b))
    .map(([ticker, item]) => ({
      id: ticker, name: ticker, value: usd(item.price.close), move: signedPct(item.price.change_1d_pct),
      lean: leanOf(item.price.change_1d_pct), spark: sparkOf(item),
      swing: item.technicals && given(item.technicals.atr14_pct) ? pct(item.technicals.atr14_pct) : null,
    }));
  const series = headline(market).map((item) => ({
    id: item.id, name: item.name, value: item.value, move: item.moves[0].text, lean: item.moves[0].lean, spark: null, swing: null,
  }));
  return [...tickers, ...series];
}

/**
 * A figure an analyst cited, with what the snapshot holds beside it whenever the two are not the same number.
 * (The pipeline accepts a cited figure that is within about two per cent of the snapshot's, so one it accepted
 * may still differ a little: the reader is shown both.)
 */
export function citationText(cited) {
  const metric = word("metric", cited.metric), value = num(cited.value, 2);
  if (cited.actual === null || cited.actual === undefined) return t`${metric} ${value}（快照里没有这项）`;
  const actual = num(cited.actual, 2);
  return actual === value ? `${metric} ${value}` : t`${metric} ${value}（快照里是 ${actual}）`;
}
