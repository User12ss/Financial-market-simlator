// What the market screens show, worked out from a run's recorded snapshot: run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { buildTrail } from "../../dashboard/static/js/decisions.js";
import {
  citationText, focusOf, gauges, headline, lastBars, levelText, levelsOf, plot, rangePlace, readings, readouts, sectors,
  sinceText, sinceThen, sparkOf, sparkPath, tape, yieldCurve,
} from "../../dashboard/static/js/market.js";

import { MARKET, PERSONAS } from "./market_fixture.mjs";

const said = (speaker, kind, ticker, payload = {}) => ({ speaker, kind, ticker, payload });

test("the screen follows the line being said: a ticker, the manager's list, an analyst's field, or the market", () => {
  assert.deepEqual(focusOf(null, PERSONAS), { kind: "overview" });
  assert.deepEqual(focusOf(said("ticker_analyst", "view", "TMO"), PERSONAS), { kind: "ticker", ticker: "TMO" });
  assert.deepEqual(focusOf(said("risk_officer", "risk_reduced", "TMO"), PERSONAS), { kind: "ticker", ticker: "TMO" });
  // The manager's list, read as the pipeline reads it: each ticker once, trimmed and in capitals.
  const list = said("portfolio_manager", "proposals", null, { proposals: [{ ticker: " tmo " }, { ticker: "ABBV" }, { ticker: "TMO" }, { ticker: "  " }] });
  assert.deepEqual(focusOf(list, PERSONAS), { kind: "list", tickers: ["TMO", "ABBV"] });
  assert.deepEqual(focusOf(said("portfolio_manager", "proposals", null, {}), PERSONAS), { kind: "list", tickers: [] });
  assert.deepEqual(focusOf(said("fixed_income", "assessment", null), PERSONAS), { kind: "desk", persona: "fixed_income" });
  // A figure with no field of its own, the pipeline's own lines, a speaker nobody knows: the market as a whole.
  for (const speaker of ["ticker_analyst", "pipeline", "a_stranger"]) {
    assert.deepEqual(focusOf(said(speaker, "macro_view", null), PERSONAS), { kind: "overview" });
  }
});

test("the latest bars are taken as recorded, one object a session", () => {
  const bars = MARKET.tickers.TMO.bars;
  assert.deepEqual(lastBars(bars, 2), [
    { date: "2026-11-11", open: 104, high: 110, low: 100, close: 102, volume: 100 },
    { date: "2026-11-12", open: 102, high: 104, low: 90, close: 100, volume: 25 },
  ]);
  assert.equal(lastBars(bars).length, 3);                       // fewer on record than the screen has room for
  assert.equal(lastBars(bars, Infinity)[0].date, "2026-11-10");
  assert.deepEqual(lastBars(MARKET.tickers.GONE.bars), []);
});

test("candles are placed by their recorded prices, and nothing else decides where they go", () => {
  const before = lastBars(MARKET.tickers.TMO.bars), after = lastBars(MARKET.tickers.TMO.after, Infinity);
  const drawn = plot(before, [], [], { width: 300, height: 100 });
  // Lows 90, highs 110: six per cent of that range is left free above and below.
  assert.deepEqual([drawn.low, drawn.high, drawn.bottom, drawn.cut, drawn.step], [88.8, 111.2, 80, null, 100]);
  assert.deepEqual(drawn.candles.map((bar) => [bar.x, bar.w, bar.up, bar.after]), [[50, 72, true, false], [150, 72, false, false], [250, 72, false, false]]);
  const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} is not ${b}`);
  const first = drawn.candles[0];
  close(first.yHigh, ((111.2 - 106) / 22.4) * 80);
  close(first.yLow, ((111.2 - 94) / 22.4) * 80);
  close(first.yOpen, ((111.2 - 96) / 22.4) * 80);
  close(first.yClose, ((111.2 - 104) / 22.4) * 80);
  close(drawn.y(111.2), 0);
  close(drawn.y(88.8), 80);
  // A higher price is always higher on the plot, and a candle's body lies inside its wick.
  for (const bar of drawn.candles) {
    assert.ok(bar.yHigh <= Math.min(bar.yOpen, bar.yClose) && Math.max(bar.yOpen, bar.yClose) <= bar.yLow);
  }
  // Volume: the busiest session's bar takes the whole of the strip kept for it (16% of the height).
  assert.deepEqual(drawn.candles.map((bar) => bar.yVolume), [92, 84, 96]);

  // With what came after joined on, the candles share the width and the join is marked.
  const joined = plot(before, after, [], { width: 300, height: 100 });
  assert.deepEqual([joined.step, joined.cut, joined.candles.map((bar) => bar.after)], [60, 180, [false, false, false, true, true]]);
  assert.deepEqual(joined.candles.map((bar) => bar.date), ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"]);
  assert.deepEqual(plot([], [], [], { width: 300, height: 100 }).candles, []);
  // A session with no trading at all, or bars that never moved, still gets a plot.
  const flat = plot([{ date: "d", open: 5, high: 5, low: 5, close: 5, volume: 0 }], [], [], { width: 100, height: 50 });
  assert.ok(Number.isFinite(flat.candles[0].yClose) && Number.isFinite(flat.candles[0].yVolume) && flat.high > flat.low);
});

test("a recorded level near the candles is put on their scale, and one far away is said to be off the plot", () => {
  const before = lastBars(MARKET.tickers.TMO.bars);
  const levels = [{ kind: "stop", by: "analyst", price: 80 }, { kind: "limit", by: "order", price: 115 },
    { kind: "stop", by: "order", price: 70 }, { kind: "limit", by: "order", price: 130 }];
  const drawn = plot(before, [], levels, { width: 300, height: 100 });
  // The range is 20, so a level within 12 of it widens the scale; the other two would squash the candles.
  assert.deepEqual(drawn.levels.map((level) => level.off), [null, null, "below", "above"]);
  assert.deepEqual([drawn.low, drawn.high].map((value) => Number(value.toFixed(6))), [77.9, 117.1]);
  assert.ok(Math.abs(drawn.levels[0].y - drawn.y(80)) < 1e-9 && drawn.levels[0].y < drawn.bottom && drawn.levels[1].y > 0);
  assert.deepEqual(drawn.levels.slice(2).map((level) => level.y), [null, null]);
  assert.deepEqual(drawn.levels.map(levelText), [
    "分析师建议止损 $80.00", "限价 $115.00", "订单止损 $70.00（在图下方）", "限价 $130.00（在图上方）"]);
});

test("stops and limits come on to a chart only when their line of the record has been reached", () => {
  const events = [
    /* 0 */ said("ticker_analyst", "view", "TMO", { direction: "bullish", confidence: 0.8, proposed_stop: 92.5 }),
    /* 1 */ said("ticker_analyst", "view", "ABBV", { direction: "bullish", confidence: 0.6, proposed_stop: null }),
    /* 2 */ said("portfolio_manager", "proposals", null, { proposals: [{ ticker: "TMO", action: "buy", confidence: 0.8 }, { ticker: "ABBV", action: "buy", confidence: 0.6 }] }),
    /* 3 */ said("risk_officer", "accepted", "TMO", { action: "buy", source: "portfolio_manager" }),
    /* 4 */ said("risk_officer", "accepted", "ABBV", { action: "buy", source: "portfolio_manager" }),
    /* 5 */ said("risk_officer", "risk_approved", "TMO", { request: { side: "buy", shares: 10, limit_price: 101.5, protective_stop: 93 }, decision: { verdict: "approved", approved_shares: 10 } }),
    /* 6 */ said("risk_officer", "risk_rejected", "ABBV", { request: { side: "buy", shares: 5, protective_stop: 280 }, decision: { verdict: "rejected", approved_shares: 0 } }),
  ];
  const rows = new Map(buildTrail(events).map((row) => [row.ticker, row]));
  const at = (ticker, index) => levelsOf(rows.get(ticker), events, index).map((level) => [level.kind, level.by, level.price, level.index]);
  assert.deepEqual(at("TMO", -1), []);
  assert.deepEqual(at("TMO", 0), [["stop", "analyst", 92.5, 0]]);
  assert.deepEqual(at("TMO", 4), [["stop", "analyst", 92.5, 0]]);            // the order's own stop is not known yet
  assert.deepEqual(at("TMO", 5), [["stop", "analyst", 92.5, 0], ["stop", "order", 93, 5], ["limit", "order", 101.5, 5]]);
  assert.deepEqual(at("ABBV", 6), []);                                       // no stop suggested, and the order was refused
  assert.deepEqual(levelsOf(undefined, events, 6), []);                      // a ticker with no decision today
});

test("a ticker's figures are read out as recorded, with a dash where the snapshot has none", () => {
  assert.deepEqual(readouts(MARKET.tickers.TMO), [
    { key: "close", label: "收盘", text: "$100.00", lean: "flat" },
    { key: "d1", label: "1 日", text: "−2.55%", lean: "down" },
    { key: "d5", label: "5 日", text: "+1.50%", lean: "up" },
    { key: "m1", label: "1 月", text: "0.00%", lean: "flat" },
    { key: "m3", label: "3 月", text: "—", lean: "flat" },
    { key: "atr", label: "日均波幅 ATR", text: "3.00%", lean: "flat" },
    { key: "rsi", label: "RSI 14", text: "44.4", lean: "flat" },
    { key: "range", label: "52 周区间位置", text: "50%", lean: "flat" },
  ]);
  // No indicators and no 52-week range on record: dashes, and no place in a range that is not there.
  assert.deepEqual(readouts(MARKET.tickers.ABBV).map((item) => item.text), ["$294.45", "0.00%", "—", "—", "—", "—", "—"]);
  assert.deepEqual([rangePlace({ close: 130, high_52w: 120, low_52w: 80 }), rangePlace({ close: 70, high_52w: 120, low_52w: 80 })], [1, 0]);
  assert.deepEqual([rangePlace({ close: 90, high_52w: 80, low_52w: 80 }), rangePlace({ close: 90 }), rangePlace(null)], [null, null, null]);
  // The levels drawn at the right of a chart: only those on record. A day's usual swing is the close give or take the ATR.
  assert.deepEqual(gauges(MARKET.tickers.TMO), {
    lines: [{ key: "sma20", label: "20 日均线", price: 98 }, { key: "sma200", label: "200 日均线", price: 90 }],
    band: { low: 92, high: 108 }, swing: { low: 97, high: 103 },
  });
  assert.deepEqual(gauges(MARKET.tickers.ABBV), { lines: [], band: null, swing: null });
});

test("what a ticker did after the session is the difference between two recorded closes", () => {
  const since = sinceThen(MARKET.tickers.TMO);
  assert.deepEqual({ ...since, change_pct: Number(since.change_pct.toFixed(6)) },
    { days: 2, through: "2026-11-16", from: 100, to: 105, change_pct: 5, lean: "up" });
  assert.equal(sinceText(since), "之后 2 个交易日（到 2026-11-16）：$100.00 → $105.00，+5.00%");
  assert.equal(sinceThen(MARKET.tickers.ABBV), null);            // no later session has it
  assert.equal(sinceThen({ bars: MARKET.tickers.TMO.bars, after: { through: "x", close: [] } }), null);
  assert.equal(sinceThen({ bars: MARKET.tickers.TMO.bars, after: { through: "x", close: [95] } }).lean, "down");
});

test("each recorded series is read in the units it is quoted in", () => {
  const [stocks, short, funds, cpi, jobs, spread, added] = readings(["^GSPC", "^IRX", "FEDFUNDS", "CPIAUCSL", "PAYEMS", "T10Y2Y", "NEWONE", "NOT-THERE"], MARKET);
  assert.deepEqual(stocks, {
    id: "^GSPC", name: "标普 500", as_of: "2026-11-12", value: "7,592.40",
    moves: [{ label: "1 日", text: "−0.35%", lean: "down" }, { label: "5 日", text: "+3.25%", lean: "up" }, { label: "1 月", text: "−1.64%", lean: "down" }],
  });
  // A yield is a percentage, and its moves are in points, never a percent of a percent.
  assert.deepEqual(short, {
    id: "^IRX", name: "3 个月国债", as_of: "2026-11-12", value: "4.13%",
    moves: [{ label: "1 日", text: "+0.01 点", lean: "up" }, { label: "5 日", text: "+0.14 点", lean: "up" }, { label: "1 月", text: "−0.04 点", lean: "down" }],
  });
  assert.deepEqual(funds, { id: "FEDFUNDS", name: "联邦基金利率", value: "3.88%", as_of: "2026-10-01", moves: [{ label: "较上期", text: "+0.13 点", lean: "up" }] });
  assert.deepEqual(cpi.moves, [{ label: "较上期", text: "+1.10", lean: "up" }, { label: "同比", text: "+3.01%", lean: "up" }]);
  assert.deepEqual([cpi.value, jobs.value, jobs.moves], ["331.20", "160,250 千人", [{ label: "较上期", text: "−50 千人", lean: "down" }]]);
  assert.deepEqual([spread.value, spread.moves[0].text], ["0.24 点", "−0.01 点"]);
  // A series the page has no name of its own for keeps its recorded name; one the snapshot lacks is left out.
  assert.deepEqual(added, { id: "NEWONE", name: "A series added later", value: "61.50", as_of: "2026-11-11", moves: [] });
  assert.deepEqual(readings(["^TYX"], MARKET)[0].moves.map((move) => move.text), ["—", "—", "—"]);
});

test("the sector funds are set in order of their month, the market series and the yield curve as recorded", () => {
  assert.deepEqual(sectors(MARKET).map((fund) => fund.symbol), ["XLV", "XLE", "XLK", "XYZ"]);      // no month on record: last
  assert.deepEqual(readings(["sectors", "^VIX"], MARKET).map((item) => item.name), ["医疗", "能源", "科技", "A fund with no name here", "VIX 波动率"]);
  assert.deepEqual(headline(MARKET).map((item) => item.id), ["^GSPC", "^VIX"]);
  // In order of maturity, whichever of the four are on record (the five-year is not, here).
  assert.deepEqual(yieldCurve(MARKET), [{ label: "3M", value: 4.13, text: "4.13%" }, { label: "10Y", value: 5.1, text: "5.10%" }, { label: "30Y", value: 5.43, text: "5.43%" }]);
  assert.deepEqual(yieldCurve({ market: [], macro: [] }), []);
});

test("a small line runs through the recorded closes from the first to the last", () => {
  assert.equal(sparkPath([10, 20, 15], 100, 20, 0), "M0.0,20.0 L50.0,0.0 L100.0,10.0");
  assert.equal(sparkPath([10, 20, 15], 100, 23), "M0.0,21.5 L50.0,1.5 L100.0,11.5");
  assert.equal(sparkPath([5, 5, 5], 10, 10, 0), "M0.0,0.0 L5.0,0.0 L10.0,0.0");            // it never moved: a level line
  assert.deepEqual([sparkPath([7], 10, 10), sparkPath([], 10, 10), sparkPath([null, 4], 10, 10)], [null, null, null]);
  assert.deepEqual(sparkOf(MARKET.tickers.TMO), [104, 102, 100]);
  assert.equal(sparkOf({ bars: { close: Array.from({ length: 50 }, (_, at) => at) } }).length, 30);
});

test("the tape lists every ticker with a price, in order of its name, then the market series", () => {
  assert.deepEqual(tape(MARKET), [
    { id: "ABBV", name: "ABBV", value: "$294.45", move: "0.00%", lean: "flat", spark: [294.45], swing: null },
    { id: "TMO", name: "TMO", value: "$100.00", move: "−2.55%", lean: "down", spark: [104, 102, 100], swing: "3.00%" },
    { id: "^GSPC", name: "标普 500", value: "7,592.40", move: "−0.35%", lean: "down", spark: null, swing: null },
    { id: "^VIX", name: "VIX 波动率", value: "18.70", move: "+6.92%", lean: "up", spark: null, swing: null },
  ]);
  assert.deepEqual(tape({ tickers: {}, market: [], macro: [] }), []);
});

test("a figure an analyst cited is shown beside what the snapshot holds when the two differ", () => {
  assert.equal(citationText({ metric: "rsi14", value: 59.95, actual: 59.95, matches: true }), "RSI 14 59.95");
  assert.equal(citationText({ metric: "sma20", value: 350, actual: 345.26, matches: false }), "20 日均线 350.00（快照里是 345.26）");
  // The pipeline accepts a figure that is close enough. It is still not the snapshot's number, so both are shown.
  assert.equal(citationText({ metric: "rsi14", value: 61, actual: 62.1, matches: true }), "RSI 14 61.00（快照里是 62.10）");
  assert.equal(citationText({ metric: "close", value: 100, actual: 100.004, matches: true }), "收盘价 100.00");      // the same to the cent
  assert.equal(citationText({ metric: "beta", value: 1.1, actual: null, matches: false }), "Beta 1.10（快照里没有这项）");
  assert.equal(citationText({ metric: "a_new_metric", value: 2, actual: 2, matches: true }), "a_new_metric 2.00");
});
