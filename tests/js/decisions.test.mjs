// How each decision of a session came about, read off its record: run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildTrail, fateText, firstLine, funnel, isLine, lastSaid, lean, lineAt, lineOf, outcome, outcomeText, reached, standings,
  stepText, stretches, STEPS,
} from "../../dashboard/static/js/decisions.js";

const titles = { settle: "结算", macro: "宏观", views: "观点", debate: "辩论", proposals: "提案", checks: "检查", risk: "风控", orders: "下单" };
const line = (stage, speaker, kind, ticker, payload = {}) =>
  ({ stage, stage_title: titles[stage], speaker, kind, ticker, payload, remark: `${speaker} on ${ticker || "the day"}` });
const sized = (verdict, side, requested, approved) =>
  ({ request: { side, shares: requested }, decision: { side, verdict, requested_shares: requested, approved_shares: approved } });
const proposed = (...items) => line("proposals", "portfolio_manager", "proposals", null, {
  proposals: items.map(([ticker, action, confidence]) => ({ ticker, action, confidence })),
});
const checked = (kind, ticker, action, source = "portfolio_manager") => line("checks", "risk_officer", kind, ticker, { action, source });

// One session, written out by hand the way the pipeline records one. The number in front of each line is its place
// in the record. The manager writes TMO in small letters with spaces round it and then proposes it a second time:
// the pipeline reads the first as TMO and does not count the second (pipeline/checks.py).
const SESSION = [
  /*  0 */ line("settle", "trader", "fill", "AMD", { side: "sell", shares: 9, price: 503.2, pnl_pct: -11.26 }),
  /*  1 */ line("macro", "central_bank", "assessment", null, { stance: "supportive", confidence: 0.6 }),
  /*  2 */ line("macro", "forex", "assessment", null, { stance: "restrictive", confidence: 0.7 }),
  /*  3 */ line("macro", "pipeline", "macro_view", null, { risk_appetite: "risk_on", confidence: 0.74, session_date: "2026-11-16" }),
  /*  4 */ line("views", "ticker_analyst", "view", "PG", { direction: "bullish", confidence: 0.85, held: true }),
  /*  5 */ line("views", "ticker_analyst", "view", "TMO", { direction: "bullish", confidence: 0.85, held: false }),
  /*  6 */ line("views", "ticker_analyst", "view", "AAPL", { direction: "neutral", confidence: 0.5, held: true }),
  /*  7 */ line("views", "ticker_analyst", "view", "XOM", { direction: "bearish", confidence: 0.6, held: true }),
  /*  8 */ line("debate", "bull", "argument", "TMO", { round: 1 }),
  /*  9 */ line("debate", "bear", "argument", "TMO", { round: 1 }),
  /* 10 */ line("debate", "bull", "argument", "TMO", { round: 2 }),
  /* 11 */ line("debate", "judge", "ruling", "TMO", { stance: "own", confidence: 0.7 }),
  /* 12 */ line("debate", "bull", "argument", "XOM", { round: 1 }),
  /* 13 */ line("debate", "bear", "argument", "XOM", { round: 1 }),
  /* 14 */ line("debate", "judge", "ruling", "XOM", { stance: "do_not_own", confidence: 0.6 }),
  /* 15 */ proposed(["PG", "buy", 0.85], [" tmo ", "buy", 0.8], ["XOM", "sell", 0.6], ["AAPL", "hold", 0.5], ["TMO", "sell", 0.9]),
  /* 16 */ checked("rejected", "PG", "buy"),               // already held
  /* 17 */ checked("accepted", "TMO", "buy"),
  /* 18 */ checked("accepted", "XOM", "sell"),
  /* 19 */ checked("accepted", "AAPL", "hold"),
  /* 20 */ checked("rejected", "TMO", "sell"),             // the second proposal for TMO: only the first counts
  /* 21 */ checked("accepted", "ABBV", "hold", "default"), // a holding nobody proposed anything for
  /* 22 */ checked("accepted", "PG", "hold", "default"),   // a holding whose only proposal was turned back
  /* 23 */ line("risk", "risk_officer", "risk_reduced", "TMO", sized("reduced", "buy", 179, 83)),
  /* 24 */ line("risk", "risk_officer", "risk_rejected", "XOM", sized("rejected", "sell", 40, 0)),
  /* 25 */ line("orders", "trader", "order_placed", "TMO", { side: "buy", shares: 83 }),
  /* 26 */ line("orders", "trader", "order_withdrawn", "MA", { side: "buy", shares: 15 }),
];
const trail = buildTrail(SESSION);
const row = (ticker) => trail.find((item) => item.ticker === ticker);

test("every ticker the record mentions has a trail, in order of first mention", () => {
  assert.deepEqual(trail.map((item) => item.ticker), ["AMD", "PG", "TMO", "AAPL", "XOM", "ABBV", "MA"]);
  assert.deepEqual(STEPS, ["view", "debate", "ruling", "proposal", "check", "risk", "order"]);
  assert.deepEqual(buildTrail([]), []);
  // Lines about no ticker in particular (the macro stage) start no trail.
  assert.deepEqual(buildTrail(SESSION.slice(1, 4)), []);
});

test("each step holds what its line recorded and where that line is", () => {
  assert.deepEqual(row("TMO"), {
    ticker: "TMO", fills: [], withdrawn: null,
    view: { index: 5, direction: "bullish", confidence: 0.85, held: false },
    debate: { index: 8, lines: [{ index: 8, round: 1 }, { index: 9, round: 1 }, { index: 10, round: 2 }] },
    ruling: { index: 11, stance: "own", confidence: 0.7 },
    proposal: { index: 15, action: "buy", confidence: 0.8 }, repeats: 1,
    check: { index: 17, status: "accepted", action: "buy", source: "portfolio_manager", kept: null },
    risk: { index: 23, verdict: "reduced", side: "buy", requested: 179, approved: 83 },
    order: { index: 25, side: "buy", shares: 83 },
  });
  assert.deepEqual(row("AMD").fills, [{ index: 0, side: "sell", shares: 9, price: 503.2, pnl_pct: -11.26 }]);
  assert.deepEqual(row("MA").withdrawn, { index: 26, side: "buy", shares: 15 });
  // A holding nobody proposed anything for is kept by the checks' own default: no proposal of the manager's.
  assert.deepEqual([row("ABBV").proposal, row("ABBV").check],
    [null, { index: 21, status: "accepted", action: "hold", source: "default", kept: null }]);
  // A holding whose proposal was turned back keeps that check, and the default that then holds it is noted on it.
  assert.deepEqual(row("PG").check, { index: 16, status: "rejected", action: "buy", source: "portfolio_manager", kept: 22 });
  assert.deepEqual([row("PG").view.held, row("PG").debate, row("PG").risk, row("PG").repeats], [true, null, null, 0]);
});

test("a ticker in a proposal is read the way the pipeline reads it: trimmed, in capitals", () => {
  // In SESSION the manager wrote " tmo ": it is TMO's proposal, and no row of its own.
  assert.equal(trail.some((item) => item.ticker !== item.ticker.trim().toUpperCase()), false);
  assert.deepEqual(row("TMO").proposal, { index: 15, action: "buy", confidence: 0.8 });
  const written = buildTrail([proposed(["\tnvda\n", "buy", 0.7], ["Brk.b", "hold", 0.5])]);
  assert.deepEqual(written.map((item) => [item.ticker, item.proposal.action]), [["NVDA", "buy"], ["BRK.B", "hold"]]);
  // A proposal for no ticker at all is nobody's.
  assert.deepEqual(buildTrail([proposed(["   ", "buy", 0.7], ["", "sell", 0.7], [null, "hold", 0.5], [undefined, "trim", 0.5])]), []);
});

test("only the first proposal for a ticker counts, as in the pipeline", () => {
  // A hold that was accepted, then a sell for the same ticker: the sell does not count, so the holding stays.
  const kept = buildTrail([
    proposed(["UPA", "hold", 0.5], ["UPA", "sell", 0.7]),
    checked("accepted", "UPA", "hold"), checked("rejected", "UPA", "sell"),
  ]);
  assert.deepEqual([kept[0].proposal, kept[0].repeats, kept[0].check], [
    { index: 0, action: "hold", confidence: 0.5 }, 1,
    { index: 1, status: "accepted", action: "hold", source: "portfolio_manager", kept: null },
  ]);
  assert.deepEqual(outcome(kept[0]), { kind: "kept", source: "portfolio_manager" });
  assert.equal(stepText("check", kept[0].check), "接受");
  assert.deepEqual(funnel(kept), { views: 0, ruled: 0, proposed: 0, accepted: 0, sized: 0, ordered: 0, kept: 1, turned_back: 0 });

  // A buy that was accepted and ordered, with the same buy written twice: one proposal, one order.
  const bought = buildTrail([
    proposed(["UPB", "buy", 0.8], ["upb", "buy", 0.8], ["UPB ", "hold", 0.1]),
    checked("accepted", "UPB", "buy"), checked("rejected", "UPB", "buy"), checked("rejected", "UPB", "hold"),
    line("risk", "risk_officer", "risk_approved", "UPB", sized("approved", "buy", 10, 10)),
    line("orders", "trader", "order_placed", "UPB", { side: "buy", shares: 10 }),
  ]);
  assert.equal(bought.length, 1);
  assert.deepEqual([bought[0].proposal.action, bought[0].repeats, bought[0].check.status, bought[0].check.index], ["buy", 2, "accepted", 1]);
  assert.deepEqual(outcome(bought[0]), { kind: "ordered", side: "buy", shares: 10 });
  assert.deepEqual(funnel(bought), { views: 0, ruled: 0, proposed: 1, accepted: 1, sized: 1, ordered: 1, kept: 0, turned_back: 0 });
  // The repeats' checks are no line of the step: the step is the check of the proposal that counts.
  assert.deepEqual([1, 2, 3].map((index) => isLine("check", bought[0].check, index)), [true, false, false]);
  assert.equal(lineOf("check", bought[0].check), 1);

  // Both turned back, then held by default: the first one's check, with the default noted on it.
  const refused = buildTrail([
    proposed(["UPC", "buy", 0.9], ["UPC", "trim", 0.4]),
    checked("rejected", "UPC", "buy"), checked("rejected", "UPC", "trim"), checked("accepted", "UPC", "hold", "default"),
  ]);
  assert.deepEqual(refused[0].check, { index: 1, status: "rejected", action: "buy", source: "portfolio_manager", kept: 3 });
  assert.deepEqual([1, 2, 3].map((index) => outcome(refused[0], index)), [
    { kind: "check_rejected", action: "buy", kept: false }, { kind: "check_rejected", action: "buy", kept: false },
    { kind: "check_rejected", action: "buy", kept: true },
  ]);
  assert.deepEqual(funnel(refused), { views: 0, ruled: 0, proposed: 1, accepted: 0, sized: 0, ordered: 0, kept: 1, turned_back: 1 });
});

test("a step counts as reached from its own line on, and leads to its latest line so far", () => {
  const tmo = row("TMO"), pg = row("PG");
  assert.deepEqual([reached(tmo.view, 4), reached(tmo.view, 5), reached(tmo.view), reached(null)], [false, true, true, false]);
  assert.deepEqual([reached(tmo.debate, 7), reached(tmo.debate, 8)], [false, true]);       // from the first argument
  assert.deepEqual([lineOf("debate", tmo.debate), lineOf("ruling", tmo.ruling), lineOf("order", tmo.order)], [10, 11, 25]);
  // While a debate is going on, its step leads to the last thing said so far, never to a line still to come.
  assert.deepEqual([8, 9, 10, 14].map((upTo) => lineAt("debate", tmo.debate, upTo)), [8, 9, 10, 10]);
  // A turned-back proposal leads to its rejection until the default that keeps the holding has been recorded.
  assert.deepEqual([16, 21, 22, Infinity].map((upTo) => lineAt("check", pg.check, upTo)), [16, 16, 22, 22]);
  assert.equal(lineAt("check", tmo.check, 26), 17);
  assert.deepEqual([8, 9, 10, 11].map((index) => isLine("debate", tmo.debate, index)), [true, true, true, false]);
  assert.deepEqual([16, 17, 22].map((index) => isLine("check", pg.check, index)), [true, false, true]);
  assert.deepEqual([17, 20].map((index) => isLine("check", tmo.check, index)), [true, false]);     // 20 checks the repeat
  assert.deepEqual([5, 6].map((index) => isLine("view", tmo.view, index)), [true, false]);
});

test("a ticker's decision begins at its first step, and a ticker with only old orders has none", () => {
  assert.deepEqual(trail.map((item) => [item.ticker, firstLine(item)]), [
    ["AMD", null], ["PG", 4], ["TMO", 5], ["AAPL", 6], ["XOM", 7], ["ABBV", 21], ["MA", null],
  ]);
});

test("where a ticker stands follows the record line by line", () => {
  const at = (ticker, index) => outcome(row(ticker), index);
  assert.deepEqual(at("TMO", 4), { kind: "waiting" });
  assert.deepEqual(at("TMO", 5), { kind: "viewed", direction: "bullish" });
  assert.deepEqual(at("TMO", 9), { kind: "debating" });
  assert.deepEqual(at("TMO", 11), { kind: "ruled", stance: "own" });
  assert.deepEqual(at("TMO", 15), { kind: "proposed", action: "buy" });
  assert.deepEqual(at("TMO", 17), { kind: "accepted", action: "buy" });
  assert.deepEqual(at("TMO", 20), { kind: "accepted", action: "buy" });                       // the repeat changes nothing
  assert.deepEqual(at("TMO", 23), { kind: "sized", verdict: "reduced", side: "buy", shares: 83 });
  assert.deepEqual(at("TMO", 25), { kind: "ordered", side: "buy", shares: 83 });
  assert.deepEqual(outcome(row("TMO")), { kind: "ordered", side: "buy", shares: 83 });        // where it ended
  // PG's buy is turned back at line 16; that it is held all the same is only known from line 22.
  assert.deepEqual([15, 16, 21, 22].map((index) => at("PG", index)), [
    { kind: "proposed", action: "buy" }, { kind: "check_rejected", action: "buy", kept: false },
    { kind: "check_rejected", action: "buy", kept: false }, { kind: "check_rejected", action: "buy", kept: true },
  ]);
});

test("every way a ticker's day can end is told apart", () => {
  const ended = Object.fromEntries(trail.map((item) => [item.ticker, outcome(item)]));
  assert.deepEqual(ended, {
    AMD: { kind: "waiting" },                                      // only a fill of an earlier order: no decision today
    PG: { kind: "check_rejected", action: "buy", kept: true },
    TMO: { kind: "ordered", side: "buy", shares: 83 },
    AAPL: { kind: "kept", source: "portfolio_manager" },
    XOM: { kind: "risk_rejected", side: "sell" },
    ABBV: { kind: "kept", source: "default" },
    MA: { kind: "waiting" },
  });
  assert.deepEqual(Object.values(ended).map((result) => outcomeText(result, true)), [
    "还没轮到", "买入的提案被规则退回，按规则继续持有", "已下单：买入 83 股", "继续持有，不动", "风控拒绝，没有下单",
    "没有提案，按规则继续持有", "还没轮到",
  ]);
});

test("a ticker is only said to have got no further once the record is over", () => {
  // [where it stands, while the record is still being played, at the end of a finished record]
  const cases = [
    [{ kind: "viewed", direction: "bearish" }, "分析师看空", "分析师看空，之后没有进一步动作"],
    [{ kind: "debating" }, "正在辩论", "辩论过，没有裁决"],
    [{ kind: "ruled", stance: "do_not_own" }, "裁判裁定不持有", "裁判裁定不持有，之后没有进一步动作"],
    [{ kind: "proposed", action: "trim" }, "组合经理提出减仓", "组合经理提出减仓，之后没有进一步动作"],
    [{ kind: "accepted", action: "sell" }, "卖出的提案通过检查", "卖出的提案通过检查，之后没有进一步动作"],
    [{ kind: "sized", verdict: "approved", side: "sell", shares: 16 }, "风控批准：卖出 16 股", "风控批准：卖出 16 股，这一次没有新的下单记录"],
    // Ends that are ends whenever they are said.
    [{ kind: "check_rejected", action: "buy", kept: false }, "买入的提案被规则退回", "买入的提案被规则退回"],
    [{ kind: "check_rejected", action: "buy", kept: true }, "买入的提案被规则退回，按规则继续持有", "买入的提案被规则退回，按规则继续持有"],
    [{ kind: "risk_rejected", side: "sell" }, "风控拒绝，没有下单", "风控拒绝，没有下单"],
    [{ kind: "ordered", side: "sell", shares: 9 }, "已下单：卖出 9 股", "已下单：卖出 9 股"],
    [{ kind: "kept", source: "default" }, "没有提案，按规则继续持有", "没有提案，按规则继续持有"],
    [{ kind: "waiting" }, "还没轮到", "还没轮到"],
  ];
  for (const [result, playing, over] of cases) {
    assert.equal(outcomeText(result, false), playing);
    assert.equal(outcomeText(result), playing);                   // not over unless said to be
    assert.equal(outcomeText(result, true), over);
  }
});

test("the funnel counts how far the tickers have got, line by line", () => {
  const none = { views: 0, ruled: 0, proposed: 0, accepted: 0, sized: 0, ordered: 0, kept: 0, turned_back: 0 };
  assert.deepEqual(funnel(trail, -1), none);
  assert.deepEqual(funnel(trail, 6), { ...none, views: 3 });
  assert.deepEqual(funnel(trail, 11), { ...none, views: 4, ruled: 1 });
  // The manager's list has five entries: three of them would change the book, one is a hold, one a repeat.
  assert.deepEqual(funnel(trail, 15), { ...none, views: 4, ruled: 2, proposed: 3 });
  assert.deepEqual(funnel(trail, 16), { ...none, views: 4, ruled: 2, proposed: 3, turned_back: 1 });
  assert.deepEqual(funnel(trail, 19), { ...none, views: 4, ruled: 2, proposed: 3, accepted: 2, kept: 1, turned_back: 1 });
  assert.deepEqual(funnel(trail, 20), funnel(trail, 19));         // the repeat's rejection is not counted
  assert.deepEqual(funnel(trail, 21), { ...none, views: 4, ruled: 2, proposed: 3, accepted: 2, kept: 2, turned_back: 1 });
  assert.deepEqual(funnel(trail, 22), { ...none, views: 4, ruled: 2, proposed: 3, accepted: 2, kept: 3, turned_back: 1 });
  assert.deepEqual(funnel(trail, 23), { views: 4, ruled: 2, proposed: 3, accepted: 2, sized: 1, ordered: 0, kept: 3, turned_back: 1 });
  assert.deepEqual(funnel(trail, 24), { views: 4, ruled: 2, proposed: 3, accepted: 2, sized: 1, ordered: 0, kept: 3, turned_back: 2 });
  assert.deepEqual(funnel(trail), { views: 4, ruled: 2, proposed: 3, accepted: 2, sized: 1, ordered: 1, kept: 3, turned_back: 2 });
});

test("a step is put in a few words made of recorded fields and numbers", () => {
  const said = (ticker) => Object.fromEntries(STEPS.filter((name) => row(ticker)[name]).map((name) => [name, stepText(name, row(ticker)[name])]));
  assert.deepEqual(said("TMO"), {
    view: "看多 0.85", debate: "2 轮 · 3 次发言", ruling: "持有 0.70", proposal: "买入 0.80", check: "接受",
    risk: "削减 179→83 股", order: "买入 83 股",
  });
  assert.deepEqual(said("XOM"), {
    view: "看空 0.60", debate: "1 轮 · 2 次发言", ruling: "不持有 0.60", proposal: "卖出 0.60", check: "接受", risk: "拒绝",
  });
  assert.deepEqual(said("PG"), { view: "看多 0.85", proposal: "买入 0.85", check: "拒绝，按规则继续持有" });
  assert.deepEqual(said("ABBV"), { check: "默认持有" });
  assert.equal(stepText("risk", { verdict: "approved", requested: 9, approved: 9 }), "批准 9 股");
  assert.equal(stepText("view", { direction: "neutral", confidence: undefined }), "中性");
  // The trail's cells are narrow: the longest wordings have a short form, with the same numbers.
  assert.equal(stepText("debate", row("TMO").debate, { brief: true }), "2 轮");
  assert.equal(stepText("risk", row("TMO").risk, { brief: true }), "削减到 83 股");
  assert.equal(stepText("order", row("TMO").order, { brief: true }), "买入 83 股");
  assert.equal(stepText("check", row("PG").check, { brief: true }), "拒绝·默认持有");
});

test("what a step says goes no further than the line the playback is at", () => {
  const debate = row("TMO").debate, check = row("PG").check;
  assert.deepEqual([8, 9, 10, 26].map((upTo) => stepText("debate", debate, { upTo })),
    ["1 轮 · 1 次发言", "1 轮 · 2 次发言", "2 轮 · 3 次发言", "2 轮 · 3 次发言"]);
  assert.deepEqual([9, 10].map((upTo) => stepText("debate", debate, { brief: true, upTo })), ["1 轮", "2 轮"]);
  // PG's buy was turned back at line 16. That the holding is kept is recorded at line 22, and said from then on.
  assert.deepEqual([16, 21, 22].map((upTo) => stepText("check", check, { upTo })), ["拒绝", "拒绝", "拒绝，按规则继续持有"]);
  assert.deepEqual([21, 22].map((upTo) => stepText("check", check, { brief: true, upTo })), ["拒绝", "拒绝·默认持有"]);
});

test("a step leans up, down, neither, or is a stop", () => {
  const leans = (ticker) => STEPS.filter((name) => row(ticker)[name]).map((name) => lean(name, row(ticker)[name]));
  assert.deepEqual(leans("TMO"), ["up", "flat", "up", "up", "flat", "flat", "up"]);
  assert.deepEqual(leans("XOM"), ["down", "flat", "down", "down", "flat", "stop"]);
  assert.deepEqual(leans("PG"), ["up", "up", "stop"]);
  assert.deepEqual(leans("AAPL"), ["flat", "flat", "flat"]);
  assert.equal(lean("order", { side: "sell" }), "down");
  assert.equal(lean("proposal", { action: "trim" }), "down");
});

test("the sign over each figure says what it has concluded so far, and changes as the record goes on", () => {
  assert.deepEqual(standings(SESSION, -1), { signs: {}, board: null });
  assert.deepEqual(standings(SESSION, 2), {
    signs: {
      trader: { text: "成交 1 · 下单 0", lean: "flat" },             // the fill the session opened with
      central_bank: { text: "偏多 0.60", lean: "up" }, forex: { text: "偏空 0.70", lean: "down" },
    },
    board: null,
  });
  assert.deepEqual(standings(SESSION, 3).board, { text: "偏进取 0.74", lean: "up", reused: null });
  assert.deepEqual(standings(SESSION, 5).signs.ticker_analyst, { text: "TMO 看多 0.85", lean: "up" });
  assert.deepEqual(standings(SESSION, 7).signs.ticker_analyst, { text: "XOM 看空 0.60", lean: "down" });
  const debating = standings(SESSION, 10).signs;
  assert.deepEqual([debating.bull, debating.bear, debating.judge], [
    { text: "TMO 第 2 轮", lean: "flat" }, { text: "TMO 第 1 轮", lean: "flat" }, undefined]);
  assert.deepEqual(standings(SESSION, 11).signs.judge, { text: "TMO 持有 0.70", lean: "up" });
  assert.deepEqual(standings(SESSION, 14).signs.judge, { text: "XOM 不持有 0.60", lean: "down" });
  // The manager's sign counts its list as it was written: all five entries, the repeat among them.
  assert.deepEqual(standings(SESSION, 15).signs.portfolio_manager, { text: "买入 2 · 卖出 2 · 持有 1", lean: "flat" });
  // The risk officer first checks the proposals, then sizes the orders: its sign counts whichever it is doing.
  assert.equal(standings(SESSION, 16).signs.risk_officer.text, "通过 0 · 退回 1");
  assert.equal(standings(SESSION, 20).signs.risk_officer.text, "通过 3 · 退回 2");
  assert.equal(standings(SESSION, 22).signs.risk_officer.text, "通过 5 · 退回 2");
  assert.equal(standings(SESSION, 23).signs.risk_officer.text, "批准 0 · 削减 1 · 拒绝 0");
  assert.equal(standings(SESSION, 24).signs.risk_officer.text, "批准 0 · 削减 1 · 拒绝 1");
  assert.equal(standings(SESSION, 0).signs.trader.text, "成交 1 · 下单 0");
  assert.equal(standings(SESSION, 26).signs.trader.text, "成交 1 · 下单 1");              // a withdrawal is not an order
  // Stepping back takes the later signs away again.
  assert.deepEqual(Object.keys(standings(SESSION, 4).signs).sort(), ["central_bank", "forex", "ticker_analyst", "trader"]);
});

test("the last thing a figure has said is looked for no further than the line being played", () => {
  assert.equal(lastSaid(SESSION, 7, "bull"), null);                                     // its first argument is line 8
  assert.deepEqual([8, 9, 10, 26].map((index) => SESSION.indexOf(lastSaid(SESSION, index, "bull"))), [8, 8, 10, 12]);
  assert.equal(SESSION.indexOf(lastSaid(SESSION, 999, "trader")), 26);                  // past the end: the last there is
  assert.deepEqual([lastSaid(SESSION, -1, "trader"), lastSaid([], 5, "trader"), lastSaid(SESSION, 26, "nobody")], [null, null, null]);
});

test("a macro view that was reused says which day it is from, and no proposals is said as such", () => {
  const reused = [line("macro", "pipeline", "macro_view_reused", null, { risk_appetite: "risk_off", confidence: 0.5, session_date: "2026-11-12" }),
    proposed()];
  assert.deepEqual(standings(reused, 0).board, { text: "偏防守 0.50", lean: "down", reused: "2026-11-12" });
  assert.deepEqual(standings(reused, 1).signs.portfolio_manager, { text: "没有提案", lean: "flat" });
  assert.deepEqual(buildTrail(reused), []);
});

test("one day of a ticker in the grid is put in fixed words and the table's own numbers", () => {
  const cell = (fields) => ({
    direction: "bullish", confidence: 0.8, held: false, action: null, source: null, check: null, repeats: 0,
    kept_by_default: false, verdict: null, approved_shares: null, ordered: null, ...fields,
  });
  assert.equal(fateText(cell({})), "");                                               // a view, and nothing done about it
  // An order in the signal table is one that passed the risk gate: that is all the grid says of it.
  assert.equal(fateText(cell({ action: "buy", check: "accepted", verdict: "reduced", approved_shares: 83, ordered: { side: "buy", shares: 83 } })),
    "买入 83 股，风控放行");
  assert.equal(fateText(cell({ action: "sell", check: "accepted", verdict: "rejected" })), "卖出，风控拒绝");
  assert.equal(fateText(cell({ action: "buy", check: "rejected" })), "买入被规则退回");
  assert.equal(fateText(cell({ action: "buy", check: "rejected", kept_by_default: true })), "买入被规则退回，默认持有");
  assert.equal(fateText(cell({ action: "hold", source: "default", check: "accepted" })), "没有提案，默认持有");
  assert.equal(fateText(cell({ action: "hold", source: "portfolio_manager", check: "accepted" })), "持有");
  assert.equal(fateText(cell({ action: "trim", source: "portfolio_manager", check: "accepted" })), "减仓，没有下单");
});

test("the record is cut into stretches by stage, and a debate by ticker", () => {
  assert.deepEqual(stretches(SESSION).map((part) => [part.title, part.ticker, part.from, part.to]), [
    ["结算", null, 0, 0], ["宏观", null, 1, 3], ["观点", null, 4, 7], ["辩论", "TMO", 8, 11], ["辩论", "XOM", 12, 14],
    ["提案", null, 15, 15], ["检查", null, 16, 22], ["风控", null, 23, 24], ["下单", null, 25, 26],
  ]);
  assert.deepEqual(stretches([]), []);
});
