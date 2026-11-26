// What stands round the meeting: the market on the wall behind it and the hour along its top, the team as its
// lead sees it, the debate round by round, and the chart's line of figures. Run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { install, wearing, words } from "./fake_dom.mjs";
import { MARKET } from "./market_fixture.mjs";

const { tooltip } = install();
const { marketState, marketWall, nyClock, pathsOf, spreadOf, statusStrip, wallPlot } = await import("../../dashboard/static/js/backdrop.js");
const { debateBoard, debateOf, debatedTicker, teamBoard } = await import("../../dashboard/static/js/team.js");
const { legendOf, shares } = await import("../../dashboard/static/js/screen.js");
const { meetingRoom } = await import("../../dashboard/static/js/meeting.js");

const figure = (id, title, seat, group, more = {}) => ({
  id, title, kind: "llm", group, seat, does: `${title}的职责。`, tone: seat % 7, hair: "short", extra: "none",
  emblem: "pulse", craft: ["专长一", "专长二", "专长三"], reads: [], ...more,
});
const ROSTER = [
  figure("portfolio_manager", "组合经理", 0, "决策", { extra: "tie" }), figure("ticker_analyst", "标的分析师", 1, "标的组", { hair: "bob", extra: "glasses" }),
  figure("bull", "多方", 2, "辩论", { hair: "long" }), figure("bear", "空方", 3, "辩论", { hair: "bun" }),
  figure("judge", "裁判", 4, "辩论", { extra: "scarf" }), figure("risk_officer", "风控官", 5, "执行", { kind: "rule", hair: "cap", extra: "badge" }),
  figure("trader", "交易员", 6, "执行", { kind: "rule", hair: "cap", extra: "headset" }),
];
const TITLES = { ...Object.fromEntries(ROSTER.map((persona) => [persona.id, persona.title])), quant_engineer: "量化工程师", all: "所有人" };
let seq = 0;
const said = (stage, speaker, to, kind, ticker, remark, payload = {}) => ({
  seq: seq += 1, ts: `2026-11-12T23:0${seq % 10}:07Z`, stage, stage_title: stage, speaker, speaker_title: TITLES[speaker], to, to_title: TITLES[to],
  is_llm: !["risk_officer", "trader", "quant_engineer"].includes(speaker), kind, ticker, remark, payload,
});
const EVENTS = [
  /* 0 */ said("views", "ticker_analyst", "portfolio_manager", "view", "TMO", "TMO looks ready.", { direction: "bullish", confidence: 0.8 }),
  /* 1 */ said("forecast", "quant_engineer", "all", "market_state", null, "Calm 0.28, turbulent 0.72.", { states: ["calm", "turbulent"], now: [0.28, 0.72] }),
  /* 2 */ said("debate", "bull", "bear", "argument", "TMO", "Orders are up.", { side: "bull", round: 1, strongest_evidence: "Backlog grew 12%." }),
  /* 3 */ said("debate", "bear", "bull", "argument", "TMO", "Margins are thin.", { side: "bear", round: 1, strongest_evidence: "" }),
  /* 4 */ said("debate", "bull", "bear", "argument", "TMO", "Margins are recovering.", { side: "bull", round: 2 }),
  /* 5 */ said("debate", "judge", "portfolio_manager", "ruling", "TMO", "Own it.", { stance: "own", confidence: 0.7 }),
  /* 6 */ said("proposals", "portfolio_manager", "risk_officer", "proposals", null, "One buy.", { proposals: [{ ticker: "TMO", action: "buy", confidence: 0.8 }] }),
  /* 7 */ said("checks", "risk_officer", "trader", "accepted", "TMO", "Buy TMO is accepted.", { action: "buy", source: "portfolio_manager" }),
  /* 8 */ said("orders", "trader", "all", "order_placed", "TMO", "Placed: buy 10 TMO.", { side: "buy", shares: 10 }),
];

// ── The market behind the meeting ────────────────────────────────────────────

test("the wall draws every ticker of the snapshot from one starting level, and nothing where there is no record", () => {
  const paths = pathsOf(MARKET);
  // ABBV has one close on record and GONE none: no path for either. TMO went 104, 102, 100.
  assert.deepEqual(paths.map((path) => path.ticker), ["TMO"]);
  assert.deepEqual(paths[0].moves.map((move) => Number(move.toFixed(3))), [0, -1.923, -3.846]);
  assert.equal(Number(paths[0].change.toFixed(3)), -3.846);
  assert.deepEqual(pathsOf(null), []);
  assert.deepEqual(pathsOf({ available: false, tickers: MARKET.tickers }), []);
  // Only the last of them, when there are more on record than the wall shows.
  const long = { available: true, tickers: { AAA: { bars: { close: [50, 100, 110, 121] } }, BBB: { bars: { close: [10, 9] } } } };
  assert.deepEqual(pathsOf(long, 3).map((path) => [path.ticker, path.moves.map((move) => Math.round(move))]), [["AAA", [0, 10, 21]], ["BBB", [0, -10]]]);
  assert.deepEqual(spreadOf(pathsOf(long, 3)), { best: pathsOf(long, 3)[0], worst: pathsOf(long, 3)[1], count: 2, up: 1, down: 1 });
  assert.equal(spreadOf([]), null);
});

test("on the wall all paths share one scale, end at the same edge, and the starting level is always in it", () => {
  const drawn = wallPlot([{ ticker: "AAA", moves: [0, 10, 20], change: 20 }, { ticker: "BBB", moves: [0, -5], change: -5 }], { x: 0, y: 0, width: 100, height: 50 });
  assert.deepEqual([drawn.low, drawn.high, drawn.zero], [-5, 20, 40]);
  assert.deepEqual(drawn.lines.map((line) => [line.ticker, line.lean, line.d, line.end]), [
    ["AAA", "up", "M0.0,40.0 L50.0,20.0 L100.0,0.0", [100, 0]],
    ["BBB", "down", "M50.0,40.0 L100.0,50.0", [100, 50]]]);                   // the shorter one starts later and ends with the rest
  // Every path above its start: the level they started from is still on the wall, at its foot.
  assert.equal(wallPlot([{ ticker: "AAA", moves: [0, 10], change: 10 }], { x: 0, y: 0, width: 10, height: 50 }).zero, 50);
});

test("the wall picks out the ticker being talked about, and nothing when there is none", () => {
  const wall = marketWall({ x: 0, y: 0, width: 100, height: 50 });
  wall.load(MARKET);
  const [line] = wearing(wall.node, "wall-line");
  const [tag] = wearing(wall.node, "wall-tag");
  assert.equal(line.attributes.class, "wall-line down");
  assert.equal(tag.attributes.visibility, "hidden");
  wall.focus("TMO");
  assert.ok(line.classes.has("on"));
  assert.equal(tag.attributes.visibility, "visible");
  assert.equal(words(tag), "");                                // its name and figure are in the strip along the top, not on the wall
  wall.focus("NOPE");
  assert.ok(!line.classes.has("on"));
  assert.equal(tag.attributes.visibility, "hidden");
  wall.load(null);                                             // no market data: an empty wall, not an error
  assert.deepEqual(wall.node.children, []);
});

test("the strip along the top gives the hour of the line, the cut-off, the session, and how unsettled the market is", () => {
  assert.equal(nyClock("2026-11-12T23:06:07Z"), "18:06:07");                  // New York is five hours behind in November
  assert.equal(nyClock("2026-11-12T23:06:07Z", false), "18:06");
  assert.equal(nyClock(null), null);
  assert.equal(nyClock("not a time"), null);
  assert.deepEqual(marketState(EVENTS, 0), null);              // not said yet at the first line
  assert.deepEqual(marketState(EVENTS, 8), { index: 1, states: [{ name: "平静", share: 0.28 }, { name: "动荡", share: 0.72 }] });
  assert.equal(marketState([{ kind: "market_state", payload: {} }], 0), null);

  const strip = statusStrip();
  const cells = () => wearing(strip.node, "status-cell").map(words);
  strip.update({ event: EVENTS[0], events: EVENTS, index: 0, run: { as_of: "2026-11-12T22:53:00Z" }, market: MARKET, focused: "TMO" });
  assert.deepEqual(cells(), ["会议时刻（纽约）18:01:07", "信息截止17:53", "行情截至2026-11-12 收盘", "VIX 波动率18.70 ▲ +6.92%",
    "1 只股票近 60 日▲ 0 ▼ 1 TMO −3.8% … TMO −3.8%", "正在讨论 TMO近 60 日 ▼ −3.8% · 日常波幅 3.00%"]);
  // The market's state appears once its line has been reached, and says whose estimate it is.
  strip.update({ event: EVENTS[2], events: EVENTS, index: 2, run: null, market: null, focused: null });
  assert.deepEqual(cells(), ["会议时刻（纽约）18:03:07", "市场状态（量化工程师）平静 28% · 动荡 72%"]);
  strip.update({ event: null, events: [], index: -1, run: null, market: { available: false }, focused: null });
  assert.deepEqual(cells(), ["会议时刻（纽约）—"]);
});

// ── The team and its debate ──────────────────────────────────────────────────

test("the team's table lists every seat in its group with what it has concluded so far and how often it has spoken", () => {
  const picked = [];
  const board = teamBoard({ roster: ROSTER, onPick: (id) => picked.push(id) });
  board.update(EVENTS, 5);                                     // the judge is ruling, to the portfolio manager
  assert.deepEqual(wearing(board.node, "team-group").map(words), ["决策1 席", "标的组1 席", "辩论3 席", "执行2 席"]);
  const rows = wearing(board.node, "team-row");
  assert.deepEqual(rows.map(words), ["组合经理模型—", "标的分析师模型▲ TMO 看多 0.801 句", "多方模型TMO 第 2 轮2 句", "空方模型TMO 第 1 轮1 句",
    "裁判模型▲ TMO 持有 0.701 句", "风控官规则—", "交易员规则—"]);
  assert.deepEqual(rows.map((row) => row.dataset.state), ["listening", "spoken", "spoken", "spoken", "speaking", "silent", "silent"]);
  assert.match(words(board.node), /7 个席位，到这一句为止 4 个发过言。/);
  rows[2].listeners.click();
  assert.deepEqual(picked, ["bull"]);
  board.update(EVENTS, 5, "bull");
  assert.deepEqual(rows.map((row) => row.attributes["aria-pressed"]), ["false", "false", "true", "false", "false", "false", "false"]);
  // Before anything has been said, nobody has a conclusion: a dash, never a guess.
  board.update(EVENTS, -1);
  assert.ok(rows.every((row) => row.dataset.state === "silent" && /—$/.test(words(row))));
});

test("the debate in hand is the one being argued, else the latest argued so far; its rounds keep each side's own line", () => {
  assert.equal(debatedTicker(EVENTS, 1), null);
  assert.equal(debatedTicker(EVENTS, 2), "TMO");
  assert.equal(debatedTicker(EVENTS, 8), "TMO");               // long after: still the last debate there was
  assert.deepEqual(debateOf(EVENTS, 3, "TMO"), {
    ticker: "TMO", ruling: null,
    rounds: [{ round: 1, bull: { index: 2, remark: "Orders are up.", evidence: "Backlog grew 12%." }, bear: { index: 3, remark: "Margins are thin.", evidence: "" } }],
  });
  const whole = debateOf(EVENTS, 8, "TMO");
  assert.deepEqual(whole.rounds.map((round) => [round.round, round.bull && round.bull.index, round.bear && round.bear.index]), [[1, 2, 3], [2, 4, null]]);
  assert.deepEqual(whole.ruling, { index: 5, stance: "own", confidence: 0.7, remark: "Own it." });
  assert.deepEqual(debateOf(EVENTS, 8, "NOPE"), { ticker: "NOPE", rounds: [], ruling: null });
});

test("the board shows the debate as far as the line being played, the ruling once made, and what became of it", () => {
  const sought = [];
  const board = debateBoard({ onSeek: (line) => sought.push(line) });
  board.update(EVENTS, 0);
  assert.equal(words(board.node), "多轮辩论到这一句为止，辩论还没有开始。");
  board.update(EVENTS, 4);                                     // the bull's second round; the bear has not answered it
  assert.match(words(board.node), /^多轮辩论 · TMO多方和空方一轮一轮地交锋，裁判听完后裁定。到这一句为止 2 轮，还没有裁定。/);
  assert.deepEqual(wearing(board.node, "debate-round").map(words), [
    "第 1 轮Orders are up.最有力的依据（原话）Backlog grew 12%.Margins are thin.", "第 2 轮Margins are recovering.还没有发言"]);
  assert.deepEqual(wearing(board.node, "debate-cell").map((cell) => cell.attributes.class),
    ["debate-cell bull", "debate-cell bear", "debate-cell bull now", "debate-cell bear none"]);
  assert.deepEqual(wearing(board.node, "debate-ruling"), []);
  wearing(board.node, "debate-cell")[1].listeners.click();
  assert.deepEqual(sought, [3]);
  assert.equal(wearing(board.node, "debate-cell")[3].listeners.click, undefined);       // nothing said there: nowhere to go

  board.update(EVENTS, 5);
  assert.equal(words(wearing(board.node, "debate-ruling")[0]), "裁判▲ 裁定持有把握度 0.70Own it.");
  assert.equal(wearing(board.node, "debate-ruling")[0].attributes.class, "debate-ruling now");
  assert.deepEqual(wearing(board.node, "debate-after"), []);                              // nothing has followed the ruling yet
  wearing(board.node, "debate-ruling")[0].listeners.click();
  assert.deepEqual(sought, [3, 5]);

  board.update(EVENTS, 8, true);
  assert.equal(words(wearing(board.node, "debate-after")[0]), "之后已下单：买入 10 股");
  board.update([EVENTS[0]], 0);
  assert.equal(words(board.node), "多轮辩论这一场的记录里没有辩论。");
  wearing(board.node, "debate-ruling");
  assert.ok(tooltip.hidden);
});

// ── A session in figures ─────────────────────────────────────────────────────

test("a chart's line of figures gives the session's open, high, low and close, its change from the close before, and its volume", () => {
  const bars = [
    { date: "2026-11-11", open: 104, high: 110, low: 100, close: 102, volume: 100, after: false },
    { date: "2026-11-12", open: 102, high: 104, low: 90, close: 100, volume: 25_300_000, after: false },
    { date: "2026-11-13", open: 100, high: 104, low: 99, close: 103, volume: 123_456_789, after: true },
  ];
  assert.deepEqual(legendOf(bars, 1).map(words), ["11-12", "开102.00", "高104.00", "低90.00", "收100.00", "▼ −2.00 (−1.96%)", "量2530.0 万"]);
  assert.deepEqual(legendOf(bars, 2).map(words), ["11-13 · 之后", "开100.00", "高104.00", "低99.00", "收103.00", "▲ +3.00 (+3.00%)", "量1.23 亿"]);
  // The first session on the chart has no close before it: its change is left out, not guessed.
  assert.deepEqual(legendOf(bars, 0).map(words), ["11-11", "开104.00", "高110.00", "低100.00", "收102.00", "量100"]);
  assert.deepEqual([shares(9999), shares(10000), shares(null)], ["9999", "1.0 万", "—"]);
});

// ── In the room ──────────────────────────────────────────────────────────────

test("the room has the lead's own place, and a line said to everyone is said to the lead too", () => {
  const room = meetingRoom({ roster: ROSTER });
  room.load({ arm: "fake/a", session: "2026-11-12", stands: true, today: {}, records: { as_of: null, lines: {} },
    run: { run_id: "run-1", status: "ok", live: false, started_at: "2026-11-12T23:00:00Z", seconds: 5, error: null, as_of: "2026-11-12T22:53:00Z" }, events: EVENTS });
  room.setMarket(MARKET);
  const [lead] = wearing(room.node, "lead");
  assert.equal(words(lead), "负责人席 · 你");
  room.seek(2);                                                // the bull to the bear: not the lead's line
  assert.ok(!lead.classes.has("hears"));
  // The wall follows the ticker in hand, the strip the hour, the table the seats, the board the debate.
  assert.ok(wearing(room.node, "wall-line")[0].classes.has("on"));
  assert.equal(words(wearing(room.node, "status-cell")[0]), "会议时刻（纽约）18:03:07");
  assert.equal(wearing(room.node, "team-row").filter((row) => row.dataset.state === "speaking").map(words)[0], "多方模型TMO 第 1 轮1 句");
  assert.match(words(wearing(room.node, "debate")[0]), /^多轮辩论 · TMO/);
  room.seek(8);                                                // the trader, to everyone
  assert.ok(lead.classes.has("hears"));
  // No figure has a drawn face: these are positions, and a smile would say something the record does not.
  assert.deepEqual(wearing(room.node, "mouth"), []);
  assert.equal(wearing(room.node, "voice").length, ROSTER.length + 2);       // seven in the room, and the two small ones beside the words
});

test("the keyboard moves through the record, and the transcript gives each line its time and who it was said to", () => {
  // The room listens for keys on the page as a whole, and stops when it is taken down.
  const heard = {};
  globalThis.document.addEventListener = (name, handler) => { heard[name] = handler; };
  globalThis.document.removeEventListener = (name, handler) => { if (heard[name] === handler) delete heard[name]; };
  const room = meetingRoom({ roster: ROSTER });
  room.load({ arm: "fake/a", session: "2026-11-12", stands: true, today: {}, records: { as_of: null, lines: {} },
    run: { run_id: "run-1", status: "ok", live: false, started_at: "2026-11-12T23:00:00Z", seconds: 5, error: null }, events: EVENTS });
  room.seek(2);
  const line = () => words(wearing(room.node, "count")[0]);
  const press = (key, more = {}) => {
    const event = { key, target: { tagName: "BODY" }, prevented: false, preventDefault() { this.prevented = true; }, ...more };
    heard.keydown(event);
    return event.prevented;
  };
  assert.equal(line(), "3 / 9");
  assert.ok(press("ArrowRight"));
  assert.equal(line(), "4 / 9");
  press("ArrowLeft");
  press("ArrowLeft");
  assert.equal(line(), "2 / 9");
  // Not while a control has the keys, not with a modifier held, and not a key something else has already taken.
  assert.ok(!press("ArrowRight", { target: { tagName: "SELECT" } }));
  assert.ok(!press("ArrowRight", { metaKey: true }));
  assert.ok(!press("ArrowRight", { defaultPrevented: true }));
  assert.ok(!press("a"));
  assert.equal(line(), "2 / 9");
  assert.match(words(wearing(room.node, "keys")[0]), /换句.*播放或暂停/);

  const rows = room.node.querySelectorAll(".log")[0].children;
  assert.equal(rows.length, EVENTS.length);
  assert.match(words(rows[2]), /^3\d\d:\d\d:\d\d多方 → 空方 · debate · TMOOrders are up\.$/);
  assert.match(words(rows[8]), /^9\d\d:\d\d:\d\d交易员 → 所有人 · orders · TMOPlaced: buy 10 TMO\.$/);

  room.destroy();
  assert.equal(heard.keydown, undefined);
  delete globalThis.document.addEventListener;
  delete globalThis.document.removeEventListener;
});
