// The market screen and the tape on the page: they show the recorded prices of whatever the line being played is
// about, and nothing of the record's later lines. Run with `node --test`. No browser here: the pieces are stand-ins.

import assert from "node:assert/strict";
import { test } from "node:test";

import { all, everything, install, wearing, words } from "./fake_dom.mjs";
import { MARKET, PERSONAS } from "./market_fixture.mjs";

const { tooltip } = install();
const { buildTrail } = await import("../../dashboard/static/js/decisions.js");
const { marketScreen } = await import("../../dashboard/static/js/screen.js");
const { tickerTape } = await import("../../dashboard/static/js/tape.js");

const said = (speaker, kind, ticker, payload = {}) => ({ speaker, kind, ticker, payload, remark: `${speaker} on ${ticker}` });
// A short session. The number in front of each line is its place in the record.
const EVENTS = [
  /*  0 */ said("fixed_income", "assessment", null, { stance: "neutral", confidence: 0.5 }),
  /*  1 */ said("ticker_analyst", "view", "TMO", { direction: "bullish", confidence: 0.8, held: false, proposed_stop: 92.5 }),
  /*  2 */ said("bull", "argument", "TMO", { round: 1 }),
  /*  3 */ said("judge", "ruling", "TMO", { stance: "own", confidence: 0.7 }),
  /*  4 */ said("portfolio_manager", "proposals", null, { proposals: [
    { ticker: " tmo ", action: "buy", confidence: 0.8 }, { ticker: "ABBV", action: "hold", confidence: 0.5 }] }),
  /*  5 */ said("risk_officer", "accepted", "TMO", { action: "buy", source: "portfolio_manager" }),
  /*  6 */ said("risk_officer", "risk_approved", "TMO", {
    request: { side: "buy", shares: 10, limit_price: 101.5, protective_stop: 93 },
    decision: { side: "buy", verdict: "approved", requested_shares: 10, approved_shares: 10 } }),
  /*  7 */ said("trader", "order_placed", "TMO", { side: "buy", shares: 10 }),
  /*  8 */ said("risk_officer", "accepted", "ABBV", { action: "hold", source: "portfolio_manager" }),
  /*  9 */ said("pipeline", "macro_view", null, { risk_appetite: "risk_on", confidence: 0.7 }),
  /* 10 */ said("trader", "fill", "ZZZ", { side: "sell", shares: 3, price: 9 }),
  /* 11 */ said("equity_market", "assessment", null, { stance: "supportive", confidence: 0.6 }),
];
const ROWS = new Map(buildTrail(EVENTS).map((row) => [row.ticker, row]));
const at = (index, ended = false) => ({ event: EVENTS[index], rows: ROWS, events: EVENTS, index, ended });

function screenOn(market = MARKET) {
  const screen = marketScreen({ personas: PERSONAS });
  screen.load(market);
  return screen;
}
const texts = (node, name) => wearing(node, name).map(words);
const candles = (node) => wearing(node, "candle");
const head = (screen) => screen.node.children[0].children.map(words);

test("until the run's market data has been read the screen says so, and when there is none it says why", () => {
  const screen = marketScreen({ personas: PERSONAS });
  screen.show(at(1));
  assert.match(words(screen.node), /正在读这一场的行情快照/);
  for (const [reason, told] of [["no_snapshot", /没有留下行情快照/], ["no_run", /还没有运行记录/], ["unreachable", /看板服务没有给出回答/], ["something_new", /没有留下行情快照/]]) {
    screen.load({ available: false, reason, tickers: {}, market: [], macro: [] });
    assert.match(words(screen.node), told);
    assert.deepEqual(candles(screen.node), []);
  }
});

test("a line about a ticker brings up its recorded bars, its price and how much it swings", () => {
  const screen = screenOn();
  screen.show(at(1));
  assert.deepEqual(head(screen), ["TMO", "Thermo Fisher · Healthcare", "$100.00▼ −2.55%"]);
  assert.equal(candles(screen.node).length, 3);
  assert.deepEqual(candles(screen.node).map((bar) => bar.attributes.class), ["candle up", "candle down", "candle down"]);
  assert.match(wearing(screen.node, "candles")[0].attributes["aria-label"], /^TMO 最近 3 个交易日的日 K 线，这一场的收盘价 \$100\.00$/);
  assert.deepEqual(texts(screen.node, "read"), ["5 日+1.50%", "1 月0.00%", "3 月—", "日均波幅 ATR3.00%", "RSI 1444.4", "52 周区间位置50%"]);
  assert.deepEqual(texts(screen.node, "holo-asof"), ["截至 2026-11-12 收盘 · 日线"]);
  // The recorded averages, and a day's usual swing round the close, are on the chart.
  assert.deepEqual(texts(screen.node, "avg-label"), ["MA20", "MA200"]);
  assert.equal(wearing(screen.node, "swing").length, 1);
  // Pointing at a session shows that day's open, high, low, close and volume.
  wearing(screen.node, "hit")[2].listeners.pointermove({ clientX: 5, clientY: 5 });
  assert.deepEqual(tooltip.children.map(words), ["TMO · 2026-11-12", "$102.00开盘", "$104.00最高", "$90.00最低", "$100.00收盘", "25成交量（股）"]);
  wearing(screen.node, "hit")[2].listeners.pointerleave();
  assert.equal(tooltip.hidden, true);
});

test("what the record says of a ticker comes on to the screen line by line, never ahead of it", () => {
  const screen = screenOn();
  const steps = () => texts(screen.node, "holo-step");
  const levels = () => texts(screen.node, "level-label");
  screen.show(at(1));
  assert.deepEqual([steps(), levels()], [["观点▲ 看多 0.80"], ["分析师建议止损 $92.50"]]);
  const page = everything(screen.node).join(" | ");
  for (const later of ["辩论", "裁决", "提案", "买入", "订单止损", "限价", "101.5", "93.00", "之后"]) {
    assert.equal(page.includes(later), false, later);
  }
  screen.show(at(3));
  assert.deepEqual(steps(), ["观点▲ 看多 0.80", "辩论1 轮", "裁决▲ 持有 0.70"]);
  screen.show(at(6));                                          // only the latest few steps: the trail has them all
  assert.deepEqual(steps(), ["裁决▲ 持有 0.70", "提案▲ 买入 0.80", "检查接受", "风控批准 10 股"]);
  assert.deepEqual(levels(), ["分析师建议止损 $92.50", "订单止损 $93.00", "限价 $101.50"]);
  screen.show(at(7));
  assert.deepEqual(steps().slice(-1), ["下单▲ 买入 10 股"]);
  // Going back takes it all away again.
  screen.show(at(1));
  assert.deepEqual([steps(), levels()], [["观点▲ 看多 0.80"], ["分析师建议止损 $92.50"]]);

  // A debate still going on is described by the arguments made so far, not by those to come.
  const debate = [EVENTS[1], said("bull", "argument", "TMO", { round: 1 }), said("bear", "argument", "TMO", { round: 2 })];
  const rows = new Map(buildTrail(debate).map((row) => [row.ticker, row]));
  screen.show({ event: debate[1], rows, events: debate, index: 1, ended: false });
  assert.deepEqual(steps(), ["观点▲ 看多 0.80", "辩论1 轮"]);
  screen.show({ event: debate[2], rows, events: debate, index: 2, ended: false });
  assert.deepEqual(steps(), ["观点▲ 看多 0.80", "辩论2 轮"]);
});

test("what the price did afterwards is joined on only once the record is over", () => {
  const screen = screenOn();
  screen.show(at(7));                                          // the run's last word on TMO, but the record is not over
  assert.equal(candles(screen.node).filter((bar) => bar.attributes.class.includes("after")).length, 0);
  assert.deepEqual([texts(screen.node, "note-since"), texts(screen.node, "cut-label")], [[], []]);
  assert.equal(everything(screen.node).join(" | ").includes("105"), false);          // the later close is nowhere

  screen.show(at(7, true));
  assert.deepEqual(candles(screen.node).map((bar) => bar.attributes.class),
    ["candle up", "candle down", "candle down", "candle up after", "candle up after"]);
  assert.deepEqual(texts(screen.node, "note-since"), ["之后 2 个交易日（到 2026-11-16）：$100.00 → $105.00，+5.00%"]);
  assert.deepEqual(texts(screen.node, "cut-label"), ["之后的实际走势（当时看不到）"]);
  assert.match(wearing(screen.node, "candles")[0].attributes["aria-label"], /后面接着之后 2 个交易日$/);
  wearing(screen.node, "hit")[4].listeners.pointermove({ clientX: 5, clientY: 5 });
  assert.equal(words(tooltip.children[0]), "TMO · 2026-11-16 · 这一场之后");

  // A ticker no later session has recorded: said so, and nothing is drawn after the session.
  screen.show(at(8, true));
  assert.deepEqual(head(screen).slice(0, 2), ["ABBV", "AbbVie · Healthcare"]);
  assert.match(words(screen.node), /这一场之后还没有新的交易日记录。/);
  assert.equal(candles(screen.node).length, 1);
  screen.show(at(8));
  assert.doesNotMatch(words(screen.node), /之后/);
});

test("a ticker the snapshot does not hold is said not to be there", () => {
  const screen = screenOn();
  screen.show(at(10));                                         // an old order of ZZZ filling
  assert.deepEqual(head(screen), ["ZZZ", "这只股票不在这一场的行情快照里"]);
  assert.deepEqual(candles(screen.node), []);
  screen.show({ ...at(10), event: said("trader", "fill", "GONE") });          // in the snapshot, but with no price or bars
  assert.deepEqual(head(screen), ["GONE", "这只股票不在这一场的行情快照里"]);
});

test("the manager's list puts its tickers side by side, each with what was proposed for it", () => {
  const screen = screenOn();
  screen.show(at(4));
  assert.deepEqual(head(screen), ["提案", "组合经理这份清单上的股票"]);
  const minis = wearing(screen.node, "mini");
  assert.deepEqual(minis.map(words), ["TMO▲ 买入 0.80$100.00 ▼ −2.55%", "ABBV持有 0.50$294.45 0.00%"]);
  // A small line of the recent closes where there are at least two of them.
  assert.deepEqual(minis.map((mini) => wearing(mini, "spark").length), [1, 0]);
  assert.equal(wearing(minis[0], "spark")[0].children[0].attributes.d.startsWith("M0.0,"), true);
  screen.show({ ...at(4), event: said("portfolio_manager", "proposals", null, { proposals: [] }) });
  assert.match(words(screen.node), /这份清单上没有任何股票。/);
  screen.show({ ...at(4), event: said("portfolio_manager", "proposals", null, { proposals: [{ ticker: "NOPE", action: "buy", confidence: 0.5 }] }) });
  assert.deepEqual(wearing(screen.node, "mini").map(words), ["NOPE快照里没有它"]);
});

test("a macro analyst speaking brings up the recorded series of that analyst's field", () => {
  const screen = screenOn();
  screen.show(at(0));
  assert.deepEqual(head(screen), ["债市分析师", "收益率曲线 · 期限利差 · 信用利差"]);
  assert.deepEqual(wearing(screen.node, "read-tile").map(words), [
    "3 个月国债4.13%1 日▲ +0.01 点5 日▲ +0.14 点1 月▼ −0.04 点",
    "10 年期国债5.10%1 日▲ +0.10 点5 日0.00 点1 月▼ −0.09 点",
    "10 年减 2 年利差0.24 点较上期▼ −0.01 点",
  ]);
  assert.equal(wearing(screen.node, "read-tile")[0].attributes.title, "截至 2026-11-12");
  // Two of its series are Treasury yields: the curve is drawn, from the shortest to the longest on record.
  assert.equal(wearing(screen.node, "curve")[0].attributes["aria-label"], "国债收益率曲线：3M 4.13%，10Y 5.10%，30Y 5.43%");
  assert.deepEqual(wearing(screen.node, "heat-cell"), []);
  assert.match(words(screen.node), /每位宏观分析师拿到的是同一张完整的数据表。/);

  screen.show(at(11));                                         // the equity analyst: indices and the sector funds
  assert.deepEqual(wearing(screen.node, "read-tile").map((tile) => words(tile.children[0])), ["标普 500", "VIX 波动率"]);
  assert.deepEqual(wearing(screen.node, "curve"), []);
  const cells = wearing(screen.node, "heat-cell");
  assert.deepEqual(cells.map(words), ["医疗▲ +7.97%", "能源▼ −4.88%", "科技▼ −8.75%", "A fund with no name here—"]);
  // Stronger moves are stronger in colour, and the direction is also an arrow and a class, not colour alone.
  assert.deepEqual(cells.map((cell) => [cell.attributes.class, cell.attributes.style]), [
    ["heat-cell up", "--heat: 65%"], ["heat-cell down", "--heat: 43%"], ["heat-cell down", "--heat: 70%"], ["heat-cell flat", "--heat: 10%"]]);
});

test("with nothing about a ticker or a field being said, the screen shows the market as a whole", () => {
  const screen = screenOn();
  for (const context of [at(9), { ...at(-1), event: undefined }]) {
    screen.show(context);
    assert.deepEqual(head(screen), ["大盘", "这一场用的行情快照"]);
    assert.deepEqual(wearing(screen.node, "read-tile").map((tile) => words(tile.children[0])), ["标普 500", "VIX 波动率"]);
    assert.equal(wearing(screen.node, "heat-cell").length, 4);
    assert.equal(wearing(screen.node, "curve").length, 1);
  }
});

test("the screen is drawn again only when what it shows has changed, and sweeps only when it turns to something else", () => {
  const screen = screenOn();
  const plotBox = () => wearing(screen.node, "holo-plot")[0];
  screen.show(at(1));
  const first = plotBox();
  assert.equal(screen.node.classes.has("scan"), true);
  screen.show(at(1));
  screen.show({ ...at(1) });
  assert.equal(plotBox(), first);                              // the same line again: left as it stands
  screen.show(at(2));                                          // the debate has begun: one more step to show
  assert.notEqual(plotBox(), first);
  assert.equal(screen.node.classes.has("scan"), false);        // still about TMO: no sweep, no candles rising again
  screen.show(at(4));
  assert.equal(screen.node.classes.has("scan"), true);         // now about the manager's list
  // New market data (another run in the room) is always drawn.
  screen.show(at(1));
  const before = plotBox();
  screen.load({ ...MARKET, run_id: "run-2" });
  assert.notEqual(plotBox(), before);
});

test("the tape runs the tickers and then the market series, twice over so that it has no gap", () => {
  const tape = tickerTape();
  assert.equal(tape.node.hidden, true);
  tape.load(null);
  tape.load({ available: false, reason: "no_snapshot", tickers: {}, market: [], macro: [] });
  assert.equal(tape.node.hidden, true);

  tape.load(MARKET);
  assert.equal(tape.node.hidden, false);
  assert.deepEqual(texts(tape.node, "tape-note"), ["2026-11-12 收盘"]);
  const [once, again] = wearing(tape.node, "tape-run");
  assert.deepEqual(once.children.map(words), [
    "ABBV$294.450.00%", "TMO$100.00▼ −2.55%波幅 3.00%", "标普 5007,592.40▼ −0.35%", "VIX 波动率18.70▲ +6.92%"]);
  assert.deepEqual(again.children.map(words), once.children.map(words));
  assert.deepEqual([once.attributes["aria-hidden"], again.attributes["aria-hidden"]], [undefined, "true"]);      // read out once
  assert.deepEqual(once.children.map((item) => wearing(item, "spark").length), [0, 1, 0, 0]);
  assert.equal(all(tape.node, (node) => node.attributes.class === "tape-move down").length, 4);
  const track = wearing(tape.node, "tape-track")[0];
  assert.equal(track.style["--tape-seconds"], "30s");
  // It can be stopped and started again with a button, for a reader with no pointer to rest on it.
  const pause = wearing(tape.node, "tape-pause")[0];
  assert.deepEqual([pause.tag, pause.attributes["aria-pressed"], track.classes.has("paused")], ["button", "false", false]);
  pause.listeners.click();
  assert.deepEqual([pause.attributes["aria-pressed"], pause.textContent, track.classes.has("paused")], ["true", "▶", true]);
  pause.listeners.click();
  assert.deepEqual([pause.attributes["aria-pressed"], pause.textContent, track.classes.has("paused")], ["false", "⏸", false]);

  tape.load({ ...MARKET });                                    // the same again: it runs on undisturbed
  assert.equal(wearing(tape.node, "tape-run")[0], once);
  tape.load({ ...MARKET, run_id: "run-2" });
  assert.notEqual(wearing(tape.node, "tape-run")[0], once);
  tape.load(null);
  assert.equal(tape.node.hidden, true);
});
