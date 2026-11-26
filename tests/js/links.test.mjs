// The meeting at a glance and the links between its seats: where it ended, the stages it went through, who has
// spoken to whom, and the line being played on its way across the room. Run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { install, wearing, words } from "./fake_dom.mjs";
import { MARKET } from "./market_fixture.mjs";

const { tooltip } = install();
const { decisiveLine, exchanges, stagesOf, summaryOf } = await import("../../dashboard/static/js/links.js");
const { buildTrail } = await import("../../dashboard/static/js/decisions.js");
const { meetingBrief, stageRail } = await import("../../dashboard/static/js/brief.js");
const { shortTitle, talkNetwork } = await import("../../dashboard/static/js/network.js");
const { meetingRoom, throwPath } = await import("../../dashboard/static/js/meeting.js");

const figure = (id, title, seat, more = {}) => ({
  id, title, kind: "llm", group: "宏观组", seat, does: `${title}的职责。`, tone: seat % 7, hair: "short", extra: "none",
  emblem: "pulse", craft: ["专长一", "专长二", "专长三"], reads: [], ...more,
});
const ROSTER = [
  figure("portfolio_manager", "组合经理", 0), figure("ticker_analyst", "标的分析师", 1), figure("bull", "多方", 2),
  figure("bear", "空方", 3), figure("judge", "裁判", 4), figure("risk_officer", "风控官", 5, { kind: "rule" }),
  figure("trader", "交易员", 6, { kind: "rule" }),
];
const TITLES = { ...Object.fromEntries(ROSTER.map((persona) => [persona.id, persona.title])), pipeline: "流程播报", quant_engineer: "量化工程师", all: "所有人" };
const STAGES = { macro: "宏观", views: "观点", forecast: "预测", debate: "辩论", proposals: "提案", checks: "检查", risk: "风控", orders: "下单" };
let seq = 0;
const said = (stage, speaker, to, kind, ticker, payload = {}) => ({
  seq: seq += 1, ts: "2026-11-12T21:00:00+00:00", stage, stage_title: STAGES[stage], speaker, speaker_title: TITLES[speaker],
  to, to_title: TITLES[to], is_llm: !["risk_officer", "trader", "pipeline", "quant_engineer"].includes(speaker), kind, ticker,
  remark: `${speaker} to ${to}${ticker ? ` on ${ticker}` : ""}`, payload,
});
const EVENTS = [
  /*  0 */ said("macro", "pipeline", "all", "macro_view", null, { risk_appetite: "risk_off", confidence: 0.6 }),
  /*  1 */ said("views", "ticker_analyst", "portfolio_manager", "view", "TMO", { direction: "bullish", confidence: 0.8 }),
  /*  2 */ said("views", "ticker_analyst", "portfolio_manager", "view", "KO", { direction: "bearish", confidence: 0.6, held: true }),
  /*  3 */ said("views", "ticker_analyst", "portfolio_manager", "view", "PG", { direction: "neutral", confidence: 0.5, held: true }),
  /*  4 */ said("forecast", "quant_engineer", "all", "ticker_forecast", "TMO", {}),
  /*  5 */ said("debate", "bull", "bear", "argument", "TMO", { round: 1 }),
  /*  6 */ said("debate", "bear", "bull", "argument", "TMO", { round: 1 }),
  /*  7 */ said("debate", "bull", "bear", "argument", "TMO", { round: 2 }),
  /*  8 */ said("debate", "judge", "portfolio_manager", "ruling", "TMO", { stance: "own", confidence: 0.7 }),
  /*  9 */ said("proposals", "portfolio_manager", "risk_officer", "proposals", null, { proposals: [
    { ticker: "TMO", action: "buy", confidence: 0.8 }, { ticker: "KO", action: "sell", confidence: 0.7 }, { ticker: "PG", action: "hold", confidence: 0.5 }] }),
  /* 10 */ said("checks", "risk_officer", "trader", "accepted", "TMO", { action: "buy", source: "portfolio_manager" }),
  /* 11 */ said("checks", "risk_officer", "trader", "accepted", "KO", { action: "sell", source: "portfolio_manager" }),
  /* 12 */ said("checks", "risk_officer", "trader", "accepted", "PG", { action: "hold", source: "portfolio_manager" }),
  /* 13 */ said("risk", "risk_officer", "portfolio_manager", "risk_rejected", "TMO", {
    request: { side: "buy", shares: 10 }, decision: { side: "buy", verdict: "rejected", requested_shares: 10, approved_shares: 0 } }),
  /* 14 */ said("risk", "risk_officer", "trader", "risk_approved", "KO", {
    request: { side: "sell", shares: 5 }, decision: { side: "sell", verdict: "approved", requested_shares: 5, approved_shares: 5 } }),
  /* 15 */ said("orders", "trader", "all", "order_placed", "KO", { side: "sell", shares: 5 }),
];
const MEETING = {
  arm: "fake/a", session: "2026-11-12", stands: true,
  run: { run_id: "run-1", status: "ok", live: false, started_at: "2026-11-12T21:00:00+00:00", seconds: 5, error: null },
  events: EVENTS, today: {}, records: { as_of: null, lines: {} },
};

// ── Counted off the record ───────────────────────────────────────────────────

test("who has spoken to whom is counted line by line, up to the line being played and no further", () => {
  const whole = exchanges(EVENTS);
  assert.deepEqual(whole.pairs.map((pair) => [pair.from, pair.to, pair.count, pair.last]), [
    ["risk_officer", "trader", 4, 14], ["ticker_analyst", "portfolio_manager", 3, 3], ["bull", "bear", 2, 7],
    ["risk_officer", "portfolio_manager", 1, 13], ["portfolio_manager", "risk_officer", 1, 9], ["judge", "portfolio_manager", 1, 8],
    ["bear", "bull", 1, 6]]);
  // A line said to everyone joins nobody to anybody: it is counted to its speaker.
  assert.deepEqual([...whole.calls], [["pipeline", 1], ["quant_engineer", 1], ["trader", 1]]);
  assert.equal(whole.said.get("risk_officer"), 5);
  const early = exchanges(EVENTS, 5);
  assert.deepEqual(early.pairs.map((pair) => [pair.from, pair.to, pair.count]), [["ticker_analyst", "portfolio_manager", 3], ["bull", "bear", 1]]);
  assert.equal(early.said.get("bear"), undefined);
  assert.deepEqual(exchanges(EVENTS, -1), { pairs: [], calls: new Map(), said: new Map() });
  // A line with no speaker, or said to its own speaker, joins nothing.
  assert.deepEqual(exchanges([{ speaker: null, to: "bull" }, { speaker: "bull", to: "bull" }, { speaker: "bull", to: null }]).pairs, []);
});

test("the stages come in the order they first appear, each with how far it has been played", () => {
  assert.deepEqual(stagesOf(EVENTS, 6).map((stage) => [stage.stage, stage.title, stage.from, stage.to, stage.count, stage.done, stage.state]), [
    ["macro", "宏观", 0, 0, 1, 1, "done"], ["views", "观点", 1, 3, 3, 3, "done"], ["forecast", "预测", 4, 4, 1, 1, "done"],
    ["debate", "辩论", 5, 8, 4, 2, "now"], ["proposals", "提案", 9, 9, 1, 0, "ahead"], ["checks", "检查", 10, 12, 3, 0, "ahead"],
    ["risk", "风控", 13, 14, 2, 0, "ahead"], ["orders", "下单", 15, 15, 1, 0, "ahead"]]);
  assert.deepEqual(stagesOf(EVENTS, -1).map((stage) => stage.state), Array(8).fill("ahead"));
  assert.deepEqual(stagesOf(EVENTS, 15).map((stage) => stage.state), [...Array(7).fill("done"), "now"]);
  assert.deepEqual(stagesOf([], 0), []);
  // A stage that comes back later is one stage: it runs from its first line to its last, and is "part" between them.
  const back = [EVENTS[1], EVENTS[5], EVENTS[2]];
  assert.deepEqual(stagesOf(back, 1).map((stage) => [stage.stage, stage.from, stage.to, stage.done, stage.state]),
    [["views", 0, 2, 1, "part"], ["debate", 1, 1, 1, "now"]]);
});

test("the summary sorts each ticker by where it ended, in the trail's own words", () => {
  const summary = summaryOf(EVENTS);
  assert.deepEqual(summary.board, { text: "偏防守 0.60", lean: "down", reused: null });
  assert.equal(summary.tickers, 3);
  assert.deepEqual(summary.funnel, { views: 3, ruled: 1, proposed: 2, accepted: 2, sized: 1, ordered: 1, kept: 1, turned_back: 1 });
  assert.deepEqual(summary.groups, {
    acted: [{ ticker: "KO", kind: "ordered", text: "已下单：卖出 5 股", lean: "down", line: 15 }],
    stopped: [{ ticker: "TMO", kind: "risk_rejected", text: "风控拒绝，没有下单", lean: "stop", line: 13 }],
    kept: [{ ticker: "PG", kind: "kept", text: "继续持有，不动", lean: "flat", line: 12 }],
    open: [],
  });
  // Part of the way through, a ticker that has got no further than talk is "open"; a buy that was placed leans up.
  const early = summaryOf(EVENTS.slice(0, 9), false);
  assert.deepEqual(early.groups.open.map((item) => [item.ticker, item.text, item.line]), [["TMO", "裁判裁定持有", 8], ["KO", "分析师看空", 2], ["PG", "分析师中性", 3]]);
  const bought = summaryOf([...EVENTS.slice(0, 13), said("orders", "trader", "all", "order_placed", "TMO", { side: "buy", shares: 10 })]);
  assert.deepEqual(bought.groups.acted.map((item) => [item.ticker, item.text, item.lean]), [["TMO", "已下单：买入 10 股", "up"]]);
  // A row with nothing but old orders filling is no decision of this meeting.
  assert.deepEqual(summaryOf([said("orders", "trader", "all", "fill", "ZZZ", { side: "sell", shares: 3, price: 9 })]).groups,
    { acted: [], stopped: [], kept: [], open: [] });
  assert.equal(decisiveLine(buildTrail(EVENTS)[0]), 13);
  assert.equal(decisiveLine({ view: null, debate: null, ruling: null, proposal: null, check: null, risk: null, order: null }), null);
});

test("a line thrown across the room always rises on the way, whichever way it goes", () => {
  const top = (path) => Number(path.match(/Q[\d.-]+,([\d.-]+)/)[1]);
  // The longer the throw the higher it goes, up to a ceiling: a throw across the whole room does not leave it.
  assert.equal(throwPath([100, 300], [900, 300]), "M100.0,300.0 Q500.0,150.0 900.0,300.0");
  assert.equal(top(throwPath([900, 300], [100, 300])), 150);                  // the same rise the other way
  assert.equal(top(throwPath([400, 300], [600, 300])), 230);                  // 18 and a quarter of its length and a little
  assert.ok(top(throwPath([200, 500], [800, 200])) < 350);                    // above the middle of a slanted throw
  assert.ok(top(throwPath([480, 200], [520, 210])) < 205);                    // and of a short one
  // Straight up and down it bends away from the middle of the room: to the left on the left, to the right on the right.
  assert.match(throwPath([200, 200], [200, 500]), /^M200\.0,200\.0 Q104\.0,350\.0 /);
  assert.match(throwPath([800, 500], [800, 200]), /^M800\.0,500\.0 Q896\.0,350\.0 /);
  assert.match(throwPath([10, 10], [10, 10]), /^M10\.0,10\.0 Q/);             // nowhere to go: still a path, not a division by nothing
});

// ── On the page ──────────────────────────────────────────────────────────────

test("the brief says where the meeting ended whatever line is being played, and each outcome leads to its line", () => {
  const sought = [];
  const brief = meetingBrief({ onSeek: (line) => sought.push(line) });
  brief.load(EVENTS, { run: "run-1", live: false });
  assert.equal(words(wearing(brief.node, "brief-head")[0]), "团队给你的结论宏观结论▼ 偏防守 0.60");
  assert.deepEqual(wearing(brief.node, "flow-step").map(words), ["3观点", "1裁决", "2提案", "2通过", "1放行", "1下单"]);
  assert.equal(wearing(brief.node, "flow-step")[2].attributes.title, "组合经理提出要买、卖或减仓的：2 只");
  assert.deepEqual(wearing(brief.node, "brief-group").map((group) => [group.attributes.class, words(group)]), [
    ["brief-group acted", "有动作1▼KO已下单：卖出 5 股"], ["brief-group stopped", "被拦下1✕TMO风控拒绝，没有下单"],
    ["brief-group kept", "保持不动1•PG"]]);
  const [sold, stopped, kept] = wearing(brief.node, "out");
  assert.deepEqual([sold, stopped, kept].map((chip) => chip.attributes["data-lean"]), ["down", "stop", "flat"]);
  // A chip that shows the ticker alone still says its outcome to a reader who cannot point at it.
  assert.equal(kept.attributes["aria-label"], "PG：继续持有，不动。点一下，跳到说这句话的地方");
  sold.listeners.click();
  stopped.listeners.click();
  assert.deepEqual(sought, [15, 13]);
  kept.listeners.pointermove({ clientX: 5, clientY: 5 });
  assert.match(words(tooltip), /PG继续持有，不动/);

  // A meeting that is still going is said to be so far; one with no decision says that; one with no line says that.
  brief.load(EVENTS.slice(0, 2), { run: "run-2", live: true });
  assert.equal(words(wearing(brief.node, "brief-head")[0]), "团队目前的进展宏观结论▼ 偏防守 0.60");
  assert.deepEqual(wearing(brief.node, "brief-group").map(words), ["只到讨论1•TMO"]);
  brief.load(EVENTS.slice(0, 1), { run: "run-3", live: false });
  assert.match(words(brief.node), /这一场没有对任何股票做出决定。/);
  brief.load([], { run: "run-4", live: false });
  assert.equal(words(brief.node), "团队给你的结论这一场还没有记录。");
});

test("the rail lights the stage being played, fills it as its lines are said, and leads to a stage's first line", () => {
  const sought = [];
  const rail = stageRail({ onSeek: (line) => sought.push(line) });
  rail.update(EVENTS, 6, "run-1");
  const steps = () => wearing(rail.node, "rail-step");
  assert.deepEqual(steps().map(words), ["宏观1", "观点3", "预测1", "辩论2/4", "提案1", "检查3", "风控2", "下单1"]);
  assert.deepEqual(steps().map((step) => step.dataset.state), ["done", "done", "done", "now", "ahead", "ahead", "ahead", "ahead"]);
  assert.deepEqual(steps().map((step) => step.style.width), Array(8).fill(undefined));
  assert.deepEqual(wearing(rail.node, "rail-fill").map((bar) => bar.style.width), ["100%", "100%", "100%", "50%", "0%", "0%", "0%", "0%"]);
  assert.deepEqual(steps().map((step) => step.attributes["aria-current"]), ["false", "false", "false", "step", "false", "false", "false", "false"]);
  steps()[5].listeners.click();
  assert.deepEqual(sought, [10]);
  // Moving on does not build the row again: the same steps, moved.
  const before = steps();
  rail.update(EVENTS, 9, "run-1");
  assert.deepEqual(steps(), before);
  assert.deepEqual(steps().map((step) => step.dataset.state), ["done", "done", "done", "done", "now", "ahead", "ahead", "ahead"]);
  rail.update([], -1, "run-2");
  assert.deepEqual(steps(), []);
});

test("the map joins two seats once one has spoken to the other, and lights the line being played", () => {
  const picked = [], sought = [];
  const net = talkNetwork({ roster: ROSTER, onPick: (id) => picked.push(id), onSeek: (line) => sought.push(line) });
  net.update(EVENTS, 7);                                       // the bull to the bear, a second time
  const links = wearing(net.node, "net-link");
  assert.equal(links.length, 3);                               // analyst to manager, bull to bear, bear to bull
  assert.deepEqual(links.filter((link) => link.attributes.class === "net-link now").length, 1);
  assert.equal(wearing(net.node, "net-flow").length, 1);
  // The heavier a pair, the wider its line; the heaviest is drawn last, on top.
  assert.match(links[links.length - 1].attributes.style, /--w: 2\.36px/);                 // three lines: 0.8 + 0.9 × √3
  const nodes = wearing(net.node, "net-node");
  // Seven seats, and the two speakers with no seat in the middle, as squares.
  assert.equal(nodes.length, 9);
  assert.deepEqual(nodes.filter((node) => node.attributes.class.includes("guest")).map(words), ["流程播报", "量化工程师"]);
  assert.deepEqual(nodes.filter((node) => node.attributes.class.includes("speaking")).map(words), ["多方"]);
  assert.deepEqual(nodes.filter((node) => node.attributes.class.includes("hearing")).map(words), ["空方"]);
  assert.deepEqual(nodes.filter((node) => node.attributes.class.includes("silent")).map(words), ["组合经理", "裁判", "风控官", "交易员"]);
  assert.match(wearing(net.node, "net")[0].attributes["aria-label"], /^到这一句为止，标的分析师对组合经理说了 3 句，多方对空方说了 2 句，空方对多方说了 1 句$/);
  assert.deepEqual(wearing(net.node, "net-pair").map(words), ["标的分析师→组合经理3 句", "多方→空方2 句", "空方→多方1 句"]);
  assert.equal(wearing(net.node, "net-pair")[1].attributes.class, "net-pair now");
  wearing(net.node, "net-pair")[0].listeners.click();
  assert.deepEqual(sought, [3]);
  // A seat's dot can be picked; a speaker with no seat cannot. Pointing at one says what it has said, and to whom.
  const hits = wearing(net.node, "net-hit");
  hits[2].listeners.click();
  assert.deepEqual(picked, ["bull"]);
  assert.equal(hits[7].listeners.click, undefined);
  hits[2].listeners.pointermove({ clientX: 1, clientY: 1 });
  assert.match(words(tooltip), /多方2 句到这一句为止说了2 句多方 → 空方1 句空方 → 多方/);

  // Said to everyone: no line to anybody, a wave on the speaker and a ring for each such line so far.
  net.update(EVENTS, 15);
  assert.equal(wearing(net.node, "net-link now").length, 0);
  assert.equal(wearing(net.node, "net-wave").length, 1);
  assert.equal(wearing(net.node, "net-calls").length, 3);
  net.update(EVENTS, -1);
  assert.deepEqual(wearing(net.node, "net-link"), []);
  assert.match(words(net.node), /还没有谁对谁单独说过话。/);
  assert.equal(shortTitle("商品与通胀分析师"), "商品");
  assert.equal(shortTitle("组合经理"), "组合经理");
});

test("in the room a line is thrown from its speaker to the one it was said to, once, and one said to everyone goes round", () => {
  const room = meetingRoom({ roster: ROSTER });
  room.load(MEETING);
  room.setMarket(MARKET);
  const links = () => wearing(room.node, "links")[0];
  const figureOf = (id) => room.node.querySelectorAll(".figure").find((item) => item.dataset.id === id);

  room.seek(5);                                               // the bull to the bear
  assert.deepEqual(links().children.map((child) => child.attributes.class), ["link-trace", "link-beam", "link-hit"]);
  assert.equal(links().children[0].attributes.d, links().children[1].attributes.d);
  assert.match(links().attributes.style, /--tone: var\(--tone-2\)/);                        // in the colour the bull wears
  assert.ok(figureOf("bear").classes.has("receiving") && !figureOf("bull").classes.has("receiving"));
  // Under the room the words carry who they were said to: an arrow, the listener's face, and the same in words.
  assert.equal(words(wearing(room.node, "who-name")[0]), "多方 对空方");
  assert.equal(wearing(wearing(room.node, "who-name")[0], "to-arrow").length, 1);
  assert.equal(wearing(wearing(room.node, "who-name")[0], "portrait").length, 1);

  const thrown = links().children[0];
  room.seek(5);                                               // the same line again: nothing is thrown a second time
  assert.equal(links().children[0], thrown);

  room.seek(15);                                              // the trader to everyone
  assert.deepEqual(links().children.map((child) => child.attributes.class), ["link-wave", "link-wave late"]);
  assert.ok(!figureOf("bear").classes.has("receiving"));
  assert.ok(ROSTER.filter((persona) => persona.id !== "trader").every((persona) => figureOf(persona.id).classes.has("hears")));
  assert.ok(!figureOf("trader").classes.has("hears"));
  // Each figure answers in turn, the nearest to the speaker first.
  const order = ROSTER.filter((persona) => persona.id !== "trader").map((persona) => Number(figureOf(persona.id).style["--wave"]));
  assert.deepEqual([...order].sort((a, b) => a - b), [0, 1, 2, 3, 4, 5]);
  assert.equal(wearing(wearing(room.node, "who-name")[0], "portrait").length, 0);           // everyone has no one face

  room.seek(-1);
  assert.deepEqual(links().children, []);
  assert.ok(ROSTER.every((persona) => !figureOf(persona.id).classes.has("hears")));
});

test("a speaker with no figure speaks through the screen, which says who it is and whether it is a model or code", () => {
  const room = meetingRoom({ roster: ROSTER });
  room.load(MEETING);
  room.setMarket(MARKET);
  const voice = () => wearing(room.node, "holo-voice")[0];
  const screen = () => wearing(room.node, "holo")[0];

  room.seek(4);                                               // the quant engineer, to everyone
  assert.equal(words(voice()), "量化工程师代码发言中");
  assert.ok(voice().classes.has("speaking") && screen().classes.has("voicing"));
  assert.deepEqual(wearing(room.node, "links")[0].children.map((child) => child.attributes.class), ["link-wave", "link-wave late"]);
  // The wave leaves from the middle of the screen, since nobody stands anywhere to leave from.
  assert.deepEqual([wearing(room.node, "link-wave")[0].attributes.cx, wearing(room.node, "link-wave")[0].attributes.cy].map(Number), [500, 312.8]);

  room.seek(5);                                               // a figure again: the screen says nothing of its own
  assert.equal(words(voice()), "");
  assert.ok(voice().hidden && !screen().classes.has("voicing"));

  // Spoken to, it is said to be listening, and the line lands on the screen's edge nearest the speaker.
  const asked = { ...MEETING, run: { ...MEETING.run, run_id: "run-9" },
    events: [...EVENTS, said("orders", "trader", "quant_engineer", "note", null, {})] };
  room.load(asked);
  room.seek(16);
  assert.equal(words(voice()), "量化工程师代码在听");
  assert.ok(!voice().classes.has("speaking") && !screen().classes.has("voicing"));
  const [trace] = wearing(room.node, "link-trace");
  const [, x, y] = trace.attributes.d.match(/ ([\d.]+),([\d.]+)$/);
  assert.ok(Number(x) >= 260 && Number(x) <= 740 && Number(y) >= 164.9 && Number(y) <= 460.7);
  assert.ok(Number(x) === 260 || Number(x) === 740 || Math.abs(Number(y) - 164.9) < 0.01 || Math.abs(Number(y) - 460.7) < 0.01);
});
