// The decision trail on the page: it shows what the record has got to by the line being played, and nothing of
// what comes after. Run with `node --test`. There is no browser here, so the page's pieces are stand-ins.

import assert from "node:assert/strict";
import { test } from "node:test";

import { everything, install } from "./fake_dom.mjs";

const { tooltip } = install();
const { decisionTrail } = await import("../../dashboard/static/js/trail.js");

const line = (stage, speaker, kind, ticker, payload = {}) =>
  ({ stage, stage_title: stage, speaker, kind, ticker, payload, remark: `${speaker} said line about ${ticker || "the day"}` });
const sized = (verdict, side, requested, approved) =>
  ({ request: { side, shares: requested }, decision: { side, verdict, requested_shares: requested, approved_shares: approved } });
const checked = (kind, ticker, action, source = "portfolio_manager") => line("checks", "risk_officer", kind, ticker, { action, source });

// The same session as in decisions.test.mjs: the number in front of each line is its place in the record.
const SESSION = [
  /*  0 */ line("settle", "trader", "fill", "AMD", { side: "sell", shares: 9, price: 503.2, pnl_pct: -11.26 }),
  /*  1 */ line("macro", "central_bank", "assessment", null, { stance: "supportive", confidence: 0.6 }),
  /*  2 */ line("macro", "forex", "assessment", null, { stance: "restrictive", confidence: 0.7 }),
  /*  3 */ line("macro", "pipeline", "macro_view", null, { risk_appetite: "risk_on", confidence: 0.74 }),
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
  /* 15 */ line("proposals", "portfolio_manager", "proposals", null, { proposals: [
    { ticker: "PG", action: "buy", confidence: 0.85 }, { ticker: " tmo ", action: "buy", confidence: 0.8 },
    { ticker: "XOM", action: "sell", confidence: 0.6 }, { ticker: "AAPL", action: "hold", confidence: 0.5 },
    { ticker: "TMO", action: "sell", confidence: 0.9 },
  ] }),
  /* 16 */ checked("rejected", "PG", "buy"),
  /* 17 */ checked("accepted", "TMO", "buy"),
  /* 18 */ checked("accepted", "XOM", "sell"),
  /* 19 */ checked("accepted", "AAPL", "hold"),
  /* 20 */ checked("rejected", "TMO", "sell"),
  /* 21 */ checked("accepted", "ABBV", "hold", "default"),
  /* 22 */ checked("accepted", "PG", "hold", "default"),
  /* 23 */ line("risk", "risk_officer", "risk_reduced", "TMO", sized("reduced", "buy", 179, 83)),
  /* 24 */ line("risk", "risk_officer", "risk_rejected", "XOM", sized("rejected", "sell", 40, 0)),
  /* 25 */ line("orders", "trader", "order_placed", "TMO", { side: "buy", shares: 83 }),
  /* 26 */ line("orders", "trader", "order_withdrawn", "MA", { side: "buy", shares: 15 }),
];
const LAST = SESSION.length - 1;

/** A trail with a record in it, and a note of every line a click asked for. */
function trailOf(events, { run = "run-1", live = false } = {}) {
  const sought = [];
  const trail = decisionTrail({ onSeek: (at) => sought.push(at) });
  trail.load(events, run, live);
  return Object.assign(trail, { sought });
}

/** The pieces of the trail's card. */
function pieces(trail) {
  const [top, , bar, scroll, none, aside] = trail.node.children;
  const [head, ...rows] = scroll.children[0].children;
  return { whole: top.children[1], bar, head, rows, none, aside };
}

const buttons = (root) => root.children[1].children.slice(1);          // after the rail
const wording = (button) => `${button.children[0].textContent}${button.children[1].textContent}`;

/** What a reader has in front of them: each row's ticker, chips, the cells that are shown, and where it stands. */
function read(trail) {
  return pieces(trail).rows.map((root) => {
    const [name, , end] = root.children;
    const [ticker, held] = name.children;
    const [result, again] = end.children;
    return {
      ticker: ticker.textContent,
      chips: [held, again].filter((chip) => !chip.hidden).map((chip) => chip.textContent),
      cells: buttons(root).map((button) => (button.hidden ? null : wording(button))),
      result: result.textContent,
      active: root.classes.has("active"),
      now: buttons(root).findIndex((button) => button.dataset.state === "now"),
    };
  });
}

const rowOf = (trail, ticker) => pieces(trail).rows.find((root) => root.children[0].children[0].textContent === ticker);
const cellOf = (trail, ticker, at) => buttons(rowOf(trail, ticker))[at];

const VIEW = 0, DEBATE = 1, RULING = 2, PROPOSAL = 3, CHECK = 4, RISK = 5, ORDER = 6;

test("before anything is said about a ticker, the trail is empty and says so", () => {
  const trail = trailOf(SESSION);
  trail.update(-1);
  const { head, rows, none, aside } = pieces(trail);
  assert.deepEqual([rows.length, head.hidden, none.hidden, none.textContent, aside.hidden],
    [0, true, false, "到这一句为止，还没有哪只股票走到观点这一步。", true]);
  trail.update(3);                                              // the macro stage: still about no ticker
  assert.equal(pieces(trail).rows.length, 0);
  assert.equal(pieces(trail).aside.textContent, "这一场开头成交的旧订单：AMD 卖出 9 股（−11.26%）。");

  const blank = trailOf([]);
  blank.update(-1);
  assert.deepEqual([pieces(blank).none.hidden, pieces(blank).none.textContent], [false, "这一场还没有记录。"]);
});

test("a ticker gets its row when it is first mentioned, and a cell when its step has happened", () => {
  const trail = trailOf(SESSION);
  trail.update(4);
  assert.deepEqual(read(trail), [{
    ticker: "PG", chips: ["持仓"], cells: ["▲ 看多 0.85", null, null, null, null, null, null], result: "分析师看多",
    active: true, now: VIEW,
  }]);
  assert.deepEqual([pieces(trail).head.hidden, pieces(trail).none.hidden], [false, true]);

  trail.update(9);
  assert.deepEqual(read(trail), [
    { ticker: "PG", chips: ["持仓"], cells: ["▲ 看多 0.85", null, null, null, null, null, null], result: "分析师看多", active: false, now: -1 },
    { ticker: "TMO", chips: [], cells: ["▲ 看多 0.85", "1 轮", null, null, null, null, null], result: "正在辩论", active: true, now: DEBATE },
    { ticker: "AAPL", chips: ["持仓"], cells: ["中性 0.50", null, null, null, null, null, null], result: "分析师中性", active: false, now: -1 },
    { ticker: "XOM", chips: ["持仓"], cells: ["▼ 看空 0.60", null, null, null, null, null, null], result: "分析师看空", active: false, now: -1 },
  ]);

  // The manager's list is one line about four of the tickers: each of them is at its proposal.
  trail.update(15);
  assert.deepEqual(read(trail).map((row) => [row.ticker, row.cells[PROPOSAL], row.result, row.active, row.now]), [
    ["PG", "▲ 买入 0.85", "组合经理提出买入", true, PROPOSAL], ["TMO", "▲ 买入 0.80", "组合经理提出买入", true, PROPOSAL],
    ["AAPL", "持有 0.50", "组合经理提出持有", true, PROPOSAL], ["XOM", "▼ 卖出 0.60", "组合经理提出卖出", true, PROPOSAL],
  ]);
  assert.deepEqual(read(trail)[1].chips, ["另有 1 条重复提案不算"]);                    // TMO is on the list twice

  // The check of TMO's repeated proposal is about TMO, and is no step of its trail.
  trail.update(20);
  assert.deepEqual([read(trail)[1].active, read(trail)[1].now, read(trail)[1].cells[CHECK]], [true, -1, "接受"]);
});

test("at the end of a finished record the trail tells how every decision came out", () => {
  const trail = trailOf(SESSION);
  trail.update(LAST);
  assert.deepEqual(read(trail), [
    {
      ticker: "PG", chips: ["持仓"], cells: ["▲ 看多 0.85", null, null, "▲ 买入 0.85", "✕ 拒绝·默认持有", null, null],
      result: "买入的提案被规则退回，按规则继续持有", active: false, now: -1,
    },
    {
      ticker: "TMO", chips: ["另有 1 条重复提案不算"],
      cells: ["▲ 看多 0.85", "2 轮", "▲ 持有 0.70", "▲ 买入 0.80", "接受", "削减到 83 股", "▲ 买入 83 股"],
      result: "已下单：买入 83 股", active: false, now: -1,
    },
    {
      ticker: "AAPL", chips: ["持仓"], cells: ["中性 0.50", null, null, "持有 0.50", "接受", null, null],
      result: "继续持有，不动", active: false, now: -1,
    },
    {
      ticker: "XOM", chips: ["持仓"], cells: ["▼ 看空 0.60", "1 轮", "▼ 不持有 0.60", "▼ 卖出 0.60", "接受", "✕ 拒绝", null],
      result: "风控拒绝，没有下单", active: false, now: -1,
    },
    {
      ticker: "ABBV", chips: [], cells: [null, null, null, null, "默认持有", null, null],
      result: "没有提案，按规则继续持有", active: false, now: -1,
    },
  ]);
  assert.equal(pieces(trail).aside.textContent,
    "这一场开头成交的旧订单：AMD 卖出 9 股（−11.26%）。这一场撤回的旧订单：MA 买入 15 股。");
  // The rail runs from a ticker's first step to its furthest one.
  const rail = (ticker) => rowOf(trail, ticker).children[1].children[0].style;
  assert.deepEqual([rail("TMO").left, rail("TMO").width], [`${(0.5 / 7) * 100}%`, `${(6 / 7) * 100}%`]);
  assert.deepEqual([rail("ABBV").left, rail("ABBV").width], [`${(4.5 / 7) * 100}%`, "0%"]);
  assert.deepEqual([rail("PG").left, rail("PG").width], [`${(0.5 / 7) * 100}%`, `${(4 / 7) * 100}%`]);
});

test("the rows are in the order their decisions begin, whatever was said of a ticker before", () => {
  // ZZZ is mentioned first, by an old order of its filling; AAA is the first the analyst speaks of.
  const record = [
    line("settle", "trader", "fill", "ZZZ", { side: "buy", shares: 5, price: 10, pnl_pct: null }),
    line("views", "ticker_analyst", "view", "AAA", { direction: "bullish", confidence: 0.6, held: false }),
    line("views", "ticker_analyst", "view", "ZZZ", { direction: "neutral", confidence: 0.5, held: true }),
  ];
  const trail = trailOf(record);
  trail.update(1);
  assert.deepEqual(read(trail).map((row) => row.ticker), ["AAA"]);
  trail.update(2);
  assert.deepEqual(read(trail).map((row) => row.ticker), ["AAA", "ZZZ"]);
  assert.equal(pieces(trail).aside.textContent, "这一场开头成交的旧订单：ZZZ 买入 5 股。");      // no gain or loss on a buy
  trail.update(1);
  assert.deepEqual(read(trail).map((row) => row.ticker), ["AAA"]);
  // The order does not depend on how the playback got there: going straight to a line gives the same.
  const straight = trailOf(record);
  straight.update(2);
  assert.deepEqual(read(straight).map((row) => row.ticker), ["AAA", "ZZZ"]);
});

test("a step that has not happened yet is nowhere on the page: no words, no arrow, no label, no marker", () => {
  const trail = trailOf(SESSION);
  for (let index = -1; index <= LAST; index += 1) {
    trail.update(index);
    const said = SESSION.slice(0, index + 1);
    const mentioned = new Set(said.flatMap((event) => [
      event.ticker, ...(event.payload.proposals || []).map((item) => item.ticker.trim().toUpperCase()),
    ]));
    const page = everything(trail.node).join(" | ");
    for (const ticker of ["PG", "TMO", "AAPL", "XOM", "ABBV", "MA"]) {
      assert.equal(page.includes(ticker), mentioned.has(ticker), `${ticker} at line ${index}`);
    }
    // What the later lines bring: none of it is there before its line.
    const later = [[11, "持有 0.70"], [14, "不持有"], [15, "买入 0.8"], [15, "重复"], [16, "拒绝"], [17, "接受"], [21, "默认"], [22, "拒绝·默认持有"], [22, "退回，按规则继续持有"],
      [23, "83"], [23, "削减"], [25, "下单：买入 83 股"], [25, "已下单"], [26, "撤回"]];
    for (const [from, words] of later) {
      if (index < from) assert.equal(page.replace("被退回或拒绝", "").includes(words), false, `"${words}" at line ${index}`);
    }
    for (const root of pieces(trail).rows) {
      for (const button of buttons(root).filter((item) => item.hidden)) {
        assert.deepEqual([wording(button), button.attributes["aria-label"], button.dataset], ["", undefined, {}]);
      }
    }
  }
});

test("going back shows exactly what going there straight shows", () => {
  const walked = trailOf(SESSION);
  for (let index = -1; index <= LAST; index += 1) walked.update(index);
  for (let index = LAST; index >= -1; index -= 1) {
    walked.update(index);
    const straight = trailOf(SESSION);
    straight.update(index);
    assert.deepEqual(read(walked), read(straight), `line ${index}`);
    assert.deepEqual(everything(walked.node), everything(straight.node), `line ${index}`);
  }
});

test("a cell's label is the step in full, as far as the record has got", () => {
  const trail = trailOf(SESSION);
  const label = (ticker, at) => cellOf(trail, ticker, at).attributes["aria-label"];
  trail.update(8);
  assert.deepEqual([wording(cellOf(trail, "TMO", DEBATE)), label("TMO", DEBATE)], ["1 轮", "TMO 辩论：1 轮 · 1 次发言"]);
  trail.update(9);                                              // the short wording stays; the full one moves on
  assert.deepEqual([wording(cellOf(trail, "TMO", DEBATE)), label("TMO", DEBATE)], ["1 轮", "TMO 辩论：1 轮 · 2 次发言"]);
  trail.update(10);
  assert.deepEqual([wording(cellOf(trail, "TMO", DEBATE)), label("TMO", DEBATE)], ["2 轮", "TMO 辩论：2 轮 · 3 次发言"]);
  trail.update(21);
  assert.deepEqual([wording(cellOf(trail, "PG", CHECK)), label("PG", CHECK)], ["✕ 拒绝", "PG 检查：拒绝"]);
  trail.update(22);
  assert.deepEqual([wording(cellOf(trail, "PG", CHECK)), label("PG", CHECK), cellOf(trail, "PG", CHECK).dataset],
    ["✕ 拒绝·默认持有", "PG 检查：拒绝，按规则继续持有", { lean: "stop", state: "now" }]);
  trail.update(LAST);
  assert.equal(label("TMO", RISK), "TMO 风控：削减 179→83 股");
  assert.deepEqual(cellOf(trail, "TMO", ORDER).dataset, { lean: "up", state: "done" });
  // Each cell keeps to its own column, so the cells that are missing leave their places empty.
  assert.deepEqual(buttons(rowOf(trail, "ABBV")).map((button) => button.style.gridColumn), ["1", "2", "3", "4", "5", "6", "7"]);
});

test("a ticker is said to have got no further only at the end of a run that has finished", () => {
  const short = SESSION.slice(0, 8);                             // a record that stops after the analyst's views
  const results = (trail) => read(trail).map((row) => row.result);
  const finished = trailOf(short);
  finished.update(6);                                            // not at the end yet
  assert.deepEqual(results(finished), ["分析师看多", "分析师看多", "分析师中性"]);
  finished.update(7);
  assert.deepEqual(results(finished), ["分析师看多，之后没有进一步动作", "分析师看多，之后没有进一步动作",
    "分析师中性，之后没有进一步动作", "分析师看空，之后没有进一步动作"]);

  // The same lines from a run that is still going: more may follow, so nothing is said about what did not happen.
  const going = trailOf(short, { live: true });
  going.update(7);
  assert.deepEqual(results(going), ["分析师看多", "分析师看多", "分析师中性", "分析师看空"]);
  going.load(short, "run-1", false);                             // the run has ended without another line
  going.update(7);
  assert.deepEqual(results(going)[3], "分析师看空，之后没有进一步动作");

  // In the middle of the full record nothing is final either.
  const whole = trailOf(SESSION);
  whole.update(17);
  assert.deepEqual(results(whole), ["买入的提案被规则退回", "买入的提案通过检查", "组合经理提出持有", "组合经理提出卖出"]);
});

test("the final result can be asked for at any point, and is the record played to its end", () => {
  const trail = trailOf(SESSION);
  trail.update(4);
  const { whole } = pieces(trail);
  assert.equal(whole.attributes["aria-pressed"], "false");
  whole.listeners.click();
  assert.equal(whole.attributes["aria-pressed"], "true");
  const played = trailOf(SESSION);
  played.update(LAST);
  assert.deepEqual(read(trail), read(played));
  assert.equal(pieces(trail).aside.textContent, pieces(played).aside.textContent);
  // A cell then leads to the last line of its step.
  cellOf(trail, "TMO", DEBATE).listeners.click();
  cellOf(trail, "PG", CHECK).listeners.click();
  assert.deepEqual(trail.sought, [10, 22]);
  whole.listeners.click();                                       // and back to where the playback is
  assert.deepEqual(read(trail), [{
    ticker: "PG", chips: ["持仓"], cells: ["▲ 看多 0.85", null, null, null, null, null, null], result: "分析师看多",
    active: true, now: VIEW,
  }]);
  // The final result of a record that stops after the views: nobody got further, and it says so from any line.
  const short = trailOf(SESSION.slice(0, 8));
  short.update(4);
  pieces(short).whole.listeners.click();
  assert.deepEqual(read(short).map((row) => row.result), ["分析师看多，之后没有进一步动作", "分析师看多，之后没有进一步动作",
    "分析师中性，之后没有进一步动作", "分析师看空，之后没有进一步动作"]);

  // A trail that is made to show the whole record always does, and has no button to choose.
  const fixed = decisionTrail({ onSeek: () => {}, whole: true });
  fixed.load(SESSION, "run-1", false);
  fixed.update(4);
  assert.deepEqual(read(fixed), read(played));
  assert.equal(pieces(fixed).whole, undefined);
});

test("a cell leads to the latest line of its step that has been said, and an empty place leads nowhere", () => {
  const trail = trailOf(SESSION);
  trail.update(9);
  cellOf(trail, "TMO", DEBATE).listeners.click();
  cellOf(trail, "TMO", VIEW).listeners.click();
  assert.deepEqual(trail.sought, [9, 5]);
  for (const at of [RULING, PROPOSAL, CHECK, RISK, ORDER]) cellOf(trail, "TMO", at).listeners.click();      // not there yet
  for (const at of [DEBATE, RULING, RISK, ORDER]) cellOf(trail, "PG", at).listeners.click();                // never there
  assert.deepEqual(trail.sought, [9, 5]);
  trail.update(21);
  cellOf(trail, "PG", CHECK).listeners.click();
  trail.update(22);
  cellOf(trail, "PG", CHECK).listeners.click();
  cellOf(trail, "TMO", DEBATE).listeners.click();
  assert.deepEqual(trail.sought, [9, 5, 16, 22, 10]);
});

test("pointing at a cell shows the words that were said, and at an empty place nothing", () => {
  const trail = trailOf(SESSION);
  const told = () => tooltip.children.map((part) => (part.children.length ? part.children.map((bit) => bit.textContent).join("") : part.textContent));
  trail.update(9);
  tooltip.hidden = true;
  cellOf(trail, "TMO", ORDER).listeners.pointermove({ clientX: 10, clientY: 10 });
  cellOf(trail, "TMO", RULING).listeners.focus();
  cellOf(trail, "PG", DEBATE).listeners.pointermove({ clientX: 10, clientY: 10 });
  assert.equal(tooltip.hidden, true);
  cellOf(trail, "TMO", DEBATE).listeners.pointermove({ clientX: 10, clientY: 10 });
  assert.equal(tooltip.hidden, false);
  assert.deepEqual(told(), ["TMO · 辩论 · 多方和空方", "1 轮 · 2 次发言", SESSION[9].remark]);     // the latest line so far
  cellOf(trail, "TMO", DEBATE).listeners.pointerleave();
  assert.equal(tooltip.hidden, true);
  cellOf(trail, "TMO", VIEW).listeners.focus();
  assert.deepEqual(told(), ["TMO · 观点 · 标的分析师", "看多 0.85", SESSION[5].remark]);
  cellOf(trail, "TMO", VIEW).listeners.blur();
  assert.equal(tooltip.hidden, true);
});

test("the funnel shows how many tickers have got how far by the line being played", () => {
  const trail = trailOf(SESSION);
  const counts = () => {
    const parts = pieces(trail).bar.children;
    const steps = parts.filter((part) => part.attributes.class === "funnel-step");
    return {
      numbers: steps.map((step) => Number(step.children[0].textContent)),
      widths: steps.map((step) => step.children[2].children[0].style.width),
      empty: steps.map((step) => step.classes.has("empty-step")),
      notes: parts[parts.length - 1].children.map((note) => note.textContent),
    };
  };
  trail.update(4);
  assert.deepEqual(counts(), {
    numbers: [1, 0, 0, 0, 0, 0], widths: ["100%", "0%", "0%", "0%", "0%", "0%"], empty: [false, true, true, true, true, true],
    notes: ["继续持有 0", "被退回或拒绝 0"],
  });
  trail.update(LAST);
  assert.deepEqual(counts(), {
    numbers: [4, 2, 3, 2, 1, 1], widths: ["100%", "50%", "75%", "50%", "25%", "25%"], empty: [false, false, false, false, false, false],
    notes: ["继续持有 3", "被退回或拒绝 2"],
  });
});

test("a run that is going keeps its rows as lines are added, and another run starts the trail over", () => {
  const trail = trailOf(SESSION.slice(0, 6), { run: "run-1", live: true });
  trail.update(5);
  const before = pieces(trail).rows;
  assert.deepEqual(read(trail).map((row) => row.ticker), ["PG", "TMO"]);

  trail.load(SESSION.slice(0, 10), "run-1", true);               // four more lines of the same run
  trail.update(9);
  const after = pieces(trail).rows;
  assert.deepEqual(read(trail).map((row) => [row.ticker, row.result]),
    [["PG", "分析师看多"], ["TMO", "正在辩论"], ["AAPL", "分析师中性"], ["XOM", "分析师看空"]]);
  assert.deepEqual([after[0] === before[0], after[1] === before[1]], [true, true]);         // not built again
  assert.equal(wording(buttons(after[1])[DEBATE]), "1 轮");                                  // and they read the longer record
  const result = (ticker) => rowOf(trail, ticker).children[2].children[0];
  assert.equal(result("TMO").classes.has("bump"), true);                                      // what changed is pointed out
  assert.equal(result("PG").classes.has("bump"), false);

  trail.load(SESSION.slice(0, 10), "run-1", true);               // the same again: nothing to do
  trail.update(9);
  assert.equal(pieces(trail).rows[0], before[0]);

  trail.load(SESSION.slice(0, 10), "run-2", false);              // another run, even with the same lines
  trail.update(9);
  assert.notEqual(pieces(trail).rows[0], before[0]);
  assert.deepEqual(read(trail).map((row) => row.ticker), ["PG", "TMO", "AAPL", "XOM"]);

  trail.load(SESSION.slice(4, 8).reverse(), "run-2", false);     // not the same record grown: started over
  trail.update(3);
  assert.deepEqual(read(trail).map((row) => row.ticker), ["XOM", "AAPL", "TMO", "PG"]);
});
