// A check-in of the desk, read off its record: the same trail, signs and screen as a close meeting, with the
// words that fit what a check-in does. Run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildTrail, funnel, lean, outcome, outcomeText, standings, stepText,
} from "../../dashboard/static/js/decisions.js";
import { focusOf, levelsOf, levelText } from "../../dashboard/static/js/market.js";

const line = (stage, speaker, kind, ticker, payload = {}) =>
  ({ stage, stage_title: stage, speaker, kind, ticker, payload, remark: `${speaker} on ${ticker || "the book"}` });
const news = (ticker, price) => line("briefing", "news_desk", "news", ticker, { ticker, new_items: [], trading: price });
const updated = (ticker, effect) => line("views", "ticker_analyst", "view_update", ticker, { ticker, effect, reason: "r", cited: [] });
const decided = (...items) => line("proposals", "portfolio_manager", "checkin_decisions", null, {
  decisions: items.map(([ticker, action]) => ({ ticker, action, reason: "r" })), summary: "s",
});
const checked = (kind, ticker, action, source = "portfolio_manager") => line("checks", "risk_officer", kind, ticker, { action, source });
const sized = (verdict, side, requested, approved) =>
  ({ request: { side, shares: requested }, decision: { side, verdict, requested_shares: requested, approved_shares: approved } });

// A check-in before the open: it can act. The number in front of each line is its place in the record.
const PREMARKET = [
  /*  0 */ line("briefing", "news_desk", "briefing", null, { book: ["KO", "DIS", "MA", "LLY", "PG"] }),
  /*  1 */ news("KO", null),
  /*  2 */ news("DIS", null),
  /*  3 */ news("MA", null),
  /*  4 */ news("LLY", null),
  /*  5 */ updated("KO", "broken"),
  /*  6 */ updated("DIS", "unchanged"),
  /*  7 */ updated("MA", "weakened"),
  /*  8 */ updated("LLY", "strengthened"),
  /*  9 */ decided(["KO", "sell"], ["DIS", "sell"], [" ma ", "withdraw"], ["MA", "keep"]),
  /* 10 */ checked("accepted", "KO", "sell"),
  /* 11 */ checked("rejected", "DIS", "sell"),
  /* 12 */ checked("accepted", "MA", "withdraw"),
  /* 13 */ checked("rejected", "MA", "keep"),               // the second decision for MA: only the first counts
  /* 14 */ checked("accepted", "DIS", "keep", "default"),
  /* 15 */ checked("accepted", "LLY", "keep", "default"),
  /* 16 */ line("risk", "risk_officer", "risk_approved", "KO", sized("approved", "sell", 101, 101)),
  /* 17 */ line("orders", "trader", "order_withdrawn", "MA", { side: "buy", shares: 15 }),
  /* 18 */ line("orders", "trader", "order_placed", "KO", { side: "sell", shares: 101 }),
];
// A check-in during the session: its decisions are only noted.
const MIDDAY = [
  /* 0 */ line("briefing", "news_desk", "briefing", null, {}),
  /* 1 */ news("KO", { last: 96.38, previous_close: 98.09, change_pct: -1.74 }),
  /* 2 */ news("DIS", { last: 119.2, previous_close: 118.5, change_pct: 0.59 }),
  /* 3 */ updated("KO", "weakened"),
  /* 4 */ updated("DIS", "unchanged"),
  /* 5 */ decided(["KO", "trim"], ["DIS", "keep"]),
  /* 6 */ checked("accepted", "KO", "trim"),
  /* 7 */ checked("accepted", "DIS", "keep"),
  /* 8 */ line("orders", "trader", "noted", "KO", { ticker: "KO", action: "trim" }),
];
const row = (events, ticker) => buildTrail(events).find((item) => item.ticker === ticker);
const ends = (events) => Object.fromEntries(buildTrail(events).map((item) => [item.ticker, outcomeText(outcome(item), true)]));

test("a check-in's lines are laid on the same steps as a close meeting's", () => {
  const trail = buildTrail(PREMARKET);
  assert.deepEqual(trail.map((item) => item.ticker), ["KO", "DIS", "MA", "LLY"]);
  assert.ok(trail.every((item) => item.checkin === true));
  assert.deepEqual(row(PREMARKET, "KO"), {
    ticker: "KO", fills: [], withdrawn: null, view: { index: 5, effect: "broken" }, debate: null, ruling: null,
    proposal: { index: 9, action: "sell", confidence: undefined }, repeats: 0,
    check: { index: 10, status: "accepted", action: "sell", source: "portfolio_manager", kept: null },
    risk: { index: 16, verdict: "approved", side: "sell", requested: 101, approved: 101 },
    order: { index: 18, side: "sell", shares: 101 }, checkin: true,
  });
  // The manager wrote MA twice, once in small letters: the first decision counts, as the desk's own rule has it.
  const ma = row(PREMARKET, "MA");
  assert.deepEqual([ma.proposal.action, ma.repeats, ma.check.action, ma.check.index], ["withdraw", 1, "withdraw", 12]);
  assert.deepEqual(ma.order, { index: 17, side: "buy", shares: 15, withdrawn: true });
  assert.deepEqual(ma.withdrawn, { index: 17, side: "buy", shares: 15 });
});

test("a record with no briefing is a close meeting, and nothing in it is read as a check-in", () => {
  const close = PREMARKET.slice(1).filter((event) => event.kind !== "view_update");
  assert.ok(buildTrail(close).every((item) => !("checkin" in item)));
  assert.equal(row(close, "MA").order, null, "at a close meeting a withdrawn order is not the decision's last step");
  assert.equal(outcome(row(close, "DIS")).checkin, undefined);
});

test("where each name of the book ends up at a check-in, in the desk's own words", () => {
  assert.deepEqual(ends(PREMARKET), {
    KO: "已下单：卖出 101 股",
    DIS: "卖出的决定被规则退回，保持原样",
    MA: "已撤回等待成交的买单：15 股",
    LLY: "没有决定，按规则保持原样",
  });
  assert.deepEqual(ends(MIDDAY), { KO: "记下，留给收盘会决定：减仓", DIS: "保持原样，不动" });
});

test("a check-in's trail says nothing of a step before its line has been played", () => {
  const ko = row(PREMARKET, "KO");
  const at = (index) => outcomeText(outcome(ko, index), false);
  assert.deepEqual([4, 5, 9, 10, 16, 17, 18].map(at), [
    "还没轮到", "分析师认为原来的观点不成立", "组合经理提出卖出", "卖出的提案通过检查", "风控批准：卖出 101 股",
    "风控批准：卖出 101 股", "已下单：卖出 101 股",
  ]);
  assert.equal(outcomeText(outcome(row(MIDDAY, "DIS"), 4), true), "分析师认为原来的观点不变，之后没有进一步动作");
  assert.equal(outcomeText(outcome(row(PREMARKET, "DIS"), 11), false), "卖出的决定被规则退回");
});

test("what each step of a check-in says in a cell of the trail, and which way it leans", () => {
  const ko = row(PREMARKET, "KO"), ma = row(PREMARKET, "MA"), lly = row(PREMARKET, "LLY"), noted = row(MIDDAY, "KO");
  assert.deepEqual(["view", "proposal", "check", "risk", "order"].map((name) => stepText(name, ko[name])),
    ["不成立", "卖出", "接受", "批准 101 股", "卖出 101 股"]);
  assert.equal(stepText("view", lly.view), "加强");
  assert.equal(stepText("check", lly.check), "默认保留");
  assert.equal(stepText("order", ma.order), "撤回等待成交的买单 15 股");
  assert.equal(stepText("order", ma.order, { brief: true }), "撤回 15 股");
  assert.equal(stepText("order", noted.order), "记下，留给收盘会：减仓");
  assert.equal(stepText("order", noted.order, { brief: true }), "记下·减仓");
  assert.deepEqual([ko, row(PREMARKET, "DIS"), ma, lly].map((item) => lean("view", item.view)), ["down", "flat", "down", "up"]);
  assert.deepEqual([lean("proposal", ma.proposal), lean("order", ma.order), lean("order", noted.order), lean("order", ko.order)],
    ["down", "stop", "down", "down"]);
  assert.equal(lean("order", { index: 1, noted: "keep" }), "flat");
});

test("only an order that was really placed is counted as placed", () => {
  assert.deepEqual(funnel(buildTrail(PREMARKET)), {
    views: 4, ruled: 0, proposed: 3, accepted: 2, sized: 1, ordered: 1, kept: 2, turned_back: 1,
  });
  assert.equal(funnel(buildTrail(MIDDAY)).ordered, 0, "a decision noted for the close is not an order");
  assert.equal(funnel(buildTrail(PREMARKET), 17).ordered, 0, "by the line that takes MA's buy back, nothing is placed yet");
});

test("the signs over the figures at a check-in", () => {
  const sign = (events, index) => standings(events, index).signs;
  assert.deepEqual(sign(PREMARKET, 4), {}, "the news desk has no figure, so its lines put up no sign");
  assert.deepEqual(sign(PREMARKET, 5).ticker_analyst, { text: "KO 不成立", lean: "down" });
  assert.deepEqual(sign(PREMARKET, 8).ticker_analyst, { text: "LLY 加强", lean: "up" });
  assert.deepEqual(sign(PREMARKET, 9).portfolio_manager, { text: "保留 1 · 卖出 2 · 撤回买单 1", lean: "flat" });
  assert.deepEqual(sign(PREMARKET, 15).risk_officer, { text: "通过 4 · 退回 2", lean: "flat" });
  assert.deepEqual(sign(PREMARKET, 17).trader, { text: "留给收盘会 0 · 撤回 1 · 下单 0", lean: "flat" });
  assert.deepEqual(sign(PREMARKET, 18).trader, { text: "留给收盘会 0 · 撤回 1 · 下单 1", lean: "flat" });
  assert.deepEqual(sign(MIDDAY, 8).trader, { text: "留给收盘会 1 · 撤回 0 · 下单 0", lean: "flat" });
  assert.deepEqual(sign([decided()], 0).portfolio_manager, { text: "没有决定", lean: "flat" });
  assert.equal(standings(PREMARKET, 18).board, null);
});

test("the screen follows a check-in: the ticker being spoken of, and the manager's list", () => {
  const personas = new Map();
  assert.deepEqual(focusOf(MIDDAY[0], personas), { kind: "overview" });
  assert.deepEqual(focusOf(MIDDAY[1], personas), { kind: "ticker", ticker: "KO" });
  assert.deepEqual(focusOf(MIDDAY[3], personas), { kind: "ticker", ticker: "KO" });
  assert.deepEqual(focusOf(PREMARKET[9], personas), { kind: "list", tickers: ["KO", "DIS", "MA"] });
  assert.deepEqual(focusOf(decided(), personas), { kind: "list", tickers: [] });
});

test("the price a ticker was trading at is put on its chart from the line that reported it, and not before", () => {
  const ko = row(MIDDAY, "KO");
  assert.deepEqual(levelsOf(ko, MIDDAY, 0), []);
  assert.deepEqual(levelsOf(ko, MIDDAY, 1), [{ kind: "now", by: "quote", price: 96.38, change: -1.74, index: 1 }]);
  assert.deepEqual(levelsOf(ko, MIDDAY, 8), levelsOf(ko, MIDDAY, 1), "it is put there once");
  assert.deepEqual(levelsOf(row(MIDDAY, "DIS"), MIDDAY, 8)[0].price, 119.2, "each ticker has its own");
  assert.deepEqual(levelsOf(row(PREMARKET, "KO"), PREMARKET, 18), [], "before the open there is no price for the day");
  assert.equal(levelText({ kind: "now", price: 96.38, change: -1.74, off: null }), "当天盘中报价 $96.38 ▼ −1.74%");
  assert.equal(levelText({ kind: "now", price: 119.2, change: 0.59, off: "above" }), "当天盘中报价 $119.20 ▲ +0.59%（在图上方）");
  assert.equal(levelText({ kind: "now", price: 100, change: 0, off: null }), "当天盘中报价 $100.00 0.00%");
  assert.equal(levelText({ kind: "now", price: 100, change: null, off: null }), "当天盘中报价 $100.00");
});
