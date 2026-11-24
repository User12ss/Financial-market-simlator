// The 评估 page's two cards about the forecast engine: how its forecasts held up, and what it last said. They are
// read from the journal's own record, so they are there whether or not an evaluation report has been written.
// Run with `node --test`. There is no browser and no server here: both are stand-ins.

import assert from "node:assert/strict";
import { test } from "node:test";

import { all, install } from "./fake_dom.mjs";

install();
// A chart draws itself once the page tells it how wide it is. Here it is told at once: 600 wide.
globalThis.ResizeObserver = class {
  constructor(told) { this.told = told; }
  observe() { this.told([{ contentRect: { width: 600 } }]); }
};
const { evaluationPage } = await import("../../dashboard/static/js/pages.js");

const side = (c50, c80, c95, loss, brier) => ({ coverage: { 50: c50, 80: c80, 95: c95 }, loss, histogram: [1, 1, 0, 0, 2, 0, 0, 0, 0, 0], brier });
const horizon = (sessions, low, high) => ({
  sessions, matures_on: "2026-12-08", quantiles: { 5: low - 4, 25: low, 50: (low + high) / 2, 75: high, 95: high + 4 },
  stop_touch: 0.3125, target_first: 0.22, stop_first: 0.3, adverse: { 50: 1, 75: 2, 95: 4 }, naive_stop_touch: 0.28,
});
const FORECASTS = {
  arm: "desk/fake/a", sessions: 3,
  card: {
    arm: "desk/fake/a", total: 24, scored: 6, pending: 17, unpriced: 1,
    by_horizon: [
      { label: "1 session", scored: 4, sessions: 2, stops: 4, stops_touched: 1, engine: side(0.5, 0.75, 1, 0.00412, 0.181), naive: side(0.25, 0.5, 0.75, 0.00455, 0.204) },
      { label: "5 sessions", scored: 2, sessions: 1, stops: 0, stops_touched: 0, engine: side(1, 1, 1, 0.009, null), naive: side(0.5, 1, 1, 0.0102, null) },
    ],
  },
  latest: {
    arm: "desk/fake/a", session_date: "2026-11-24", run_id: "run-2", created_at: "2026-11-24T23:30:00Z", params_version: 3,
    params_cutoff: "2026-11-20", seed: 123456, paths: 10000, states: ["calm", "turbulent"], state_now: [0.83, 0.17],
    state_ahead: { 1: [0.82, 0.18], 5: [0.78, 0.22], 10: [0.74, 0.26], 20: [0.7, 0.3] },
    tickers: [
      {
        ticker: "RISE", close: 100, beta: 0.5, sector: "XLK", own_vol: 0.01, daily_vol: 0.012, naive_vol: 0.011, stop: 96,
        stop_from: "position", target: 108, break_even: 0.3333, direction: "bullish", confidence: 0.6, view_horizon: 10, lean: 0,
        lean_basis: "0 scored view(s) held with confidence 0.60 to 0.69, 0 of them beat the benchmark: no lean yet.",
        report_on: null, report_modelled: false, notes: [], horizons: [horizon(1, 99, 101), horizon(5, 97, 103), horizon(10, 95, 106), horizon(20, 92, 110)],
      },
      {
        ticker: "BARE", close: 50, beta: 1, sector: "", own_vol: 0.01, daily_vol: 0.012, naive_vol: 0.011, stop: null, stop_from: "",
        target: null, break_even: null, direction: "", confidence: null, view_horizon: null, lean: 0, lean_basis: "", report_on: null,
        report_modelled: false, notes: [],
        horizons: [horizon(5, 48, 52)].map((item) => ({ ...item, stop_touch: null, target_first: null, stop_first: null, naive_stop_touch: null })),
      },
    ],
    book: {
      equity: 100200, cash: 92000, high_water: 100500, positions: 1, notes: [],
      horizons: [{ sessions: 10, equity: { 5: 99753.4, 25: 100000, 50: 100200, 75: 100400, 95: 100686.2 }, stops_together: 0, drawdown_breaker: 0.004, daily_loss_breaker: 0.0312 }],
    },
    problems: ["LATE: version 3 of the parameters has nothing on it: it was not in the estimate, or its history could not be used."],
  },
};
const NOTHING = { arm: "fake/a", sessions: 0, card: null, latest: null };
let answer = null;
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => answer });
const settled = async () => { for (let turn = 0; turn < 5; turn += 1) await new Promise((done) => setImmediate(done)); };
const words = (node) => node.textContent + node.children.map(words).join("");
const cards = (page) => all(page.node, (node) => node.tag === "section" && node.attributes.class === "card");
const titled = (page, title) => cards(page).find((card) => words(card.children[0]) === title);

async function shown(forecasts) {
  answer = { arm: "desk/fake/a", report: null, arms: [], lessons: [], lessons_shown: false, forecasts };
  const page = evaluationPage({ arm: "desk/fake/a" });
  await settled();
  return page;
}

test("how the forecasts held up is shown without an evaluation report, beside the plain forecast", async () => {
  const page = await shown(FORECASTS);
  assert.ok(titled(page, "还没有评估报告"));                    // the report is still missing, and said to be
  const record = words(titled(page, "预测对账"));
  assert.match(record, /量化工程师是代码，不是模型。.*这些预测现在不影响任何订单。/);
  assert.match(record, /留档的预测24来自 3 场收盘会/);
  assert.match(record, /已经对账617 条还没到期，1 条缺价格/);
  // The table under the bars holds every recorded figure of every horizon, engine first.
  const rows = all(titled(page, "预测对账"), (node) => node.tag === "tr").map((row) => row.children.map(words));
  assert.deepEqual(rows[1], ["1 个交易日后", "4", "2", "50% / 75% / 100%", "25% / 50% / 75%", "0.00412 / 0.00455", "1 / 4", "0.181 / 0.204"]);
  assert.deepEqual(rows[2], ["5 个交易日后", "2", "1", "100% / 100% / 100%", "50% / 100% / 100%", "0.00900 / 0.01020", "0 / 0", "— / —"]);
  // One bar for every horizon and band, for the engine and again for the plain forecast, each against what it should be.
  const bars = all(titled(page, "预测对账"), (node) => node.attributes.class === "bar").map((bar) => bar.attributes["aria-label"]);
  assert.equal(bars.length, 12);
  assert.equal(bars[0], "1 日 · 中间 50%：实际落在区间内 50%。4 条预测");
  assert.equal(bars[5], "5 日 · 中间 95%：实际落在区间内 100%。2 条预测");
  assert.equal(bars[6], "1 日 · 中间 50%：实际落在区间内 25%。4 条预测");
  assert.match(record, /“5 日 · 中间 80%”指 5 个交易日后的那份预测的中间 80%。/);
});

test("what the engine last said is shown as recorded: the state, each name's bands and stop, the book, what it could not do", async () => {
  const page = await shown(FORECASTS);
  const latest = titled(page, "最近一次预测");
  const text = words(latest);
  assert.match(text, /2026-11-24 的收盘会 · 参数第 3 版（数据截到 2026-11-20）· 10,000 条模拟路径 · 随机种子 123456。/);
  assert.match(text, /市场状态：平稳83%10 个交易日后 74%/);
  assert.match(text, /市场状态：动荡17%10 个交易日后 26%/);
  const rows = all(latest, (node) => node.tag === "tr").map((row) => row.children.map(words));
  assert.deepEqual(rows[1], [
    "RISE", "$100.00", "$95.00 – $106.00", "$91.00 – $110.00", "$96.00（账上的止损）", "31%", "22%",
    "0 scored view(s) held with confidence 0.60 to 0.69, 0 of them beat the benchmark: no lean yet.",
  ]);
  // A name with no stop and no ten-session horizon: its furthest horizon, and a dash where nothing was recorded.
  assert.deepEqual(rows[2], ["BARE", "$50.00", "$48.00 – $52.00", "$44.00 – $56.00", "—", "—", "—", "—"]);
  assert.match(text, /组合照现在的 1 只持仓、之后不再交易来算：10 个交易日后净值九成落在 \$99,753 到 \$100,686。/);
  assert.match(text, /同一天有两只以上触及止损的可能 0%，触及回撤熔断 0.4%，触及单日亏损熔断 3.1%。/);
  assert.match(text, /没能预测的（原文）LATE: version 3 of the parameters has nothing on it/);
  // Nothing on the page that stands for a figure the record does not hold.
  assert.doesNotMatch(words(page.node), /null|undefined|NaN/);
});

test("before any forecast has matured the page says so, and a group without forecasts has no such card", async () => {
  const early = await shown({
    ...FORECASTS, card: { arm: "desk/fake/a", total: 8, scored: 0, pending: 8, unpriced: 0, by_horizon: [] },
    latest: { ...FORECASTS.latest, book: { ...FORECASTS.latest.book, positions: 0 }, problems: [], tickers: [] },
  });
  const record = words(titled(early, "预测对账"));
  assert.match(record, /预测都还没到期。最短的期限是 1 个交易日，下一场收盘会就开始对账。/);
  assert.equal(all(titled(early, "预测对账"), (node) => node.attributes.class === "bar").length, 0);
  const latest = words(titled(early, "最近一次预测"));
  assert.match(latest, /这一场没有能预测的股票。/);
  assert.doesNotMatch(latest, /组合照现在的|没能预测的/);

  for (const none of [NOTHING, undefined, null]) {
    const page = await shown(none);
    assert.equal(titled(page, "预测对账"), undefined);
    assert.equal(titled(page, "最近一次预测"), undefined);
    assert.ok(titled(page, "复盘写下的教训"));
  }
});
