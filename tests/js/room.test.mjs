// The meeting room as a whole: the market screen in the middle follows the line being played, and each figure's
// panel shows its field and what it has concluded so far, in recorded fields and its own words. Run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { all, everything, install, wearing, words } from "./fake_dom.mjs";
import { MARKET } from "./market_fixture.mjs";

install();
const { meetingRoom } = await import("../../dashboard/static/js/meeting.js");

const figure = (id, title, seat, more = {}) => ({
  id, title, kind: "llm", group: "宏观组", seat, does: `${title}的职责。`, tone: seat % 7, hair: "short", extra: "none",
  emblem: "pulse", craft: ["专长一", "专长二", "专长三"], reads: [], ...more,
});
const ROSTER = [
  figure("portfolio_manager", "组合经理", 0, { emblem: "pie" }),
  figure("ticker_analyst", "标的分析师", 1, { emblem: "scope", craft: ["价格走势", "基本面", "技术指标", "止损位"] }),
  figure("fixed_income", "债市分析师", 2, { emblem: "curve", reads: ["^IRX", "^TNX", "T10Y2Y", "sectors"] }),
  figure("bull", "多方", 3, { emblem: "bull" }),
  figure("judge", "裁判", 4, { emblem: "scales" }),
  figure("risk_officer", "风控官", 5, { kind: "rule", emblem: "shield" }),
  figure("trader", "交易员", 6, { kind: "rule", emblem: "ticket" }),
];
const TITLES = Object.fromEntries(ROSTER.map((persona) => [persona.id, persona.title]));
let seq = 0;
const said = (stage, speaker, to, kind, ticker, remark, payload = {}) => ({
  seq: seq += 1, ts: `2026-11-12T21:00:0${seq % 10}+00:00`, stage, stage_title: stage, speaker,
  speaker_title: TITLES[speaker] || "流程播报", to, to_title: TITLES[to] || "所有人", is_llm: !["risk_officer", "trader", "pipeline"].includes(speaker),
  kind, ticker, remark, payload,
});
const EVENTS = [
  /* 0 */ said("macro", "fixed_income", "all", "assessment", null, "The curve is steepening.", {
    stance: "restrictive", confidence: 0.6, summary: "Yields rose.", key_risk: "A failed auction would hit equities.",
    sector_tilts: [{ sector: "Utilities", tilt: "underweight" }] }),
  /* 1 */ said("views", "ticker_analyst", "portfolio_manager", "view", "TMO", "TMO looks ready to turn.", {
    direction: "bullish", confidence: 0.8, held: false, horizon_days: 10, proposed_stop: 92.5,
    invalidation: "A close below the 200-day average.", thesis: "Margins are recovering.",
    evidence: [{ metric: "rsi14", value: 44.44, actual: 44.44, matches: true }, { metric: "sma20", value: 350, actual: 98, matches: false }] }),
  /* 2 */ said("debate", "bull", "judge", "argument", "TMO", "The backlog is the story.", {
    round: 1, argument: "Orders are up.", strongest_evidence: "Backlog grew 12% year over year." }),
  /* 3 */ said("debate", "judge", "portfolio_manager", "ruling", "TMO", "Own it.", {
    stance: "own", confidence: 0.7, reasoning: "The bull's backlog point was not answered.", invalidation: "Two weak quarters." }),
  /* 4 */ said("proposals", "portfolio_manager", "risk_officer", "proposals", null, "One buy today.", {
    proposals: [{ ticker: "TMO", action: "buy", confidence: 0.8, reason: "View and ruling agree." }], summary: "Keep it small." }),
  /* 5 */ said("checks", "risk_officer", "trader", "accepted", "TMO", "Buy TMO is accepted.", { action: "buy", source: "portfolio_manager" }),
  /* 6 */ said("orders", "trader", "all", "order_placed", "TMO", "Placed: buy 10 TMO.", { side: "buy", shares: 10 }),
];
const MEETING = {
  arm: "fake/a", session: "2026-11-12", stands: true,
  run: { run_id: "run-1", status: "ok", live: false, started_at: "2026-11-12T21:00:00+00:00", seconds: 5, error: null },
  events: EVENTS, today: { ticker_analyst: "1 条观点：看多 1", trader: "成交 0 笔，下单 1 张" }, records: { as_of: null, lines: {} },
};

function roomAt(line, { market = MARKET } = {}) {
  const room = meetingRoom({ roster: ROSTER });
  room.load(MEETING);
  room.setMarket(market);
  room.seek(line);
  return room;
}
const panel = (room) => wearing(room.node, "person")[0];
const holo = (room) => wearing(room.node, "holo")[0];
const pick = (room, id) => all(room.node, (node) => node.dataset.id === id)[0].listeners.click();

test("at no line of the record does anything unfilled show up in the room", () => {
  const room = roomAt(-1);
  for (let line = -1; line < EVENTS.length; line += 1) {
    room.seek(line);
    for (const id of [null, ...ROSTER.map((persona) => persona.id)]) {
      if (id) pick(room, id);
      const page = everything(room.node).join(" | ");
      assert.doesNotMatch(page, /null|undefined|NaN|\[object/, `line ${line}, ${id || "nobody"} picked`);
      if (id) pick(room, id);                                    // and let go again
    }
  }
});

test("the screen in the middle of the room follows the line being played", () => {
  const room = roomAt(1);
  assert.deepEqual(holo(room).children[0].children.map(words).slice(0, 2), ["TMO", "Thermo Fisher · Healthcare"]);
  assert.equal(wearing(holo(room), "candle").length, 3);
  room.seek(0);
  assert.equal(words(holo(room).children[0].children[0]), "债市分析师");
  room.seek(4);
  assert.equal(words(holo(room).children[0].children[0]), "提案");
  // The last line of a finished run: what the price did afterwards is joined on.
  room.seek(6);
  assert.equal(wearing(holo(room), "candle").filter((bar) => bar.attributes.class.includes("after")).length, 2);
  room.seek(5);
  assert.equal(wearing(holo(room), "candle").filter((bar) => bar.attributes.class.includes("after")).length, 0);

  // A run that is still going is not over at its latest line.
  const going = meetingRoom({ roster: ROSTER });
  going.load({ ...MEETING, run: { ...MEETING.run, status: "running", live: true } });
  going.setMarket(MARKET);
  going.seek(6);
  assert.equal(wearing(holo(going), "candle").filter((bar) => bar.attributes.class.includes("after")).length, 0);
  // Before the market data has come, and when there is none, the screen says so.
  assert.match(words(holo(roomAt(1, { market: null }))), /正在读这一场的行情快照/);
  assert.match(words(holo(roomAt(1, { market: { available: false, reason: "no_snapshot" } }))), /没有留下行情快照/);
});

test("with nobody picked, the panel is about whoever is speaking: the field, the conclusion, the own words", () => {
  const room = roomAt(1);
  const text = words(panel(room));
  assert.match(text, /^标的分析师模型宏观组标的分析师的职责。价格走势基本面技术指标止损位/);
  assert.match(text, /到这一句为止的结论TMO 看多 0\.800\.80/);                       // the sign's words, and the confidence as lamps
  assert.equal(wearing(panel(room), "gauge")[0].attributes["aria-label"], "把握度 0.80");
  assert.equal(wearing(panel(room), "gauge")[0].children.filter((lamp) => lamp.attributes.class === "on").length, 8);
  assert.match(text, /期限 10 个交易日 · 建议止损 92\.50/);
  assert.match(text, /什么情况下作废（原话）A close below the 200-day average\./);
  // What it cited, each figure checked against the snapshot by the pipeline: a tick, or a cross with what the snapshot holds.
  assert.deepEqual(wearing(panel(room), "cite").map((cited) => [cited.attributes.class, words(cited)]), [
    ["cite ok", "✓ RSI 14 44.44"], ["cite bad", "✗ 20 日均线 350.00（快照里是 98.00）"]]);
  assert.match(text, /到这一句为止的发言次数1 次/);
  assert.doesNotMatch(text, /1 条观点：看多 1/);                 // the whole session's tally is for when the record is over
  assert.match(text, /现在显示的是正在发言的人。/);

  room.seek(0);                                                  // a macro analyst: its conclusion and its field's recorded data
  const macro = words(panel(room));
  assert.match(macro, /到这一句为止的结论偏空 0\.600\.60主要风险（原话）A failed auction would hit equities\.Utilities 低配/);
  assert.match(macro, /这个领域的数据（2026-11-12 的快照）/);
  assert.deepEqual(wearing(panel(room), "field-row").map(words),
    ["3 个月国债4.13%1 日 +0.01 点", "10 年期国债5.10%1 日 +0.10 点", "10 年减 2 年利差0.24 点较上期 −0.01 点"]);
  room.seek(2);
  assert.match(words(panel(room)), /TMO 第 1 轮最有力的依据（原话）Backlog grew 12% year over year\./);
  room.seek(4);
  assert.match(words(panel(room)), /买入 1整体思路（原话）Keep it small\./);
  room.seek(6);
  assert.match(words(panel(room)), /^交易员规则/);
  assert.match(words(panel(room)), /到这一句为止的结论成交 0 · 下单 1/);
  room.seek(-1);
  assert.match(words(panel(room)), /^这些人是谁/);
});

test("a figure that is picked stays in the panel, and is shown only what it has said so far", () => {
  const room = roomAt(1);
  pick(room, "judge");
  assert.match(words(panel(room)), /^裁判/);
  assert.match(words(panel(room)), /到这一句为止，还没有发言。/);
  assert.doesNotMatch(words(panel(room)), /backlog point|持有 0\.70|Own it/);     // its ruling comes two lines later
  room.seek(3);
  assert.match(words(panel(room)), /TMO 持有 0\.700\.70裁决理由（原话）The bull's backlog point was not answered\.什么情况下反转（原话）Two weak quarters\./);
  room.seek(6);
  assert.match(words(panel(room)), /^裁判/);                     // still the judge, whoever is speaking
  assert.match(words(panel(room)), /取消选中/);
  pick(room, "judge");                                           // let go: back to the speaker
  assert.match(words(panel(room)), /^交易员/);

  // Without market data a macro analyst's panel has no field data to list, and says nothing made up in its place.
  const bare = roomAt(0, { market: { available: false, reason: "no_snapshot" } });
  assert.deepEqual(wearing(panel(bare), "field-row"), []);
  assert.doesNotMatch(words(panel(bare)), /这个领域的数据/);
});

test("a figure that speaks more than once is shown its latest words so far, never ones still to come", () => {
  const twice = [
    EVENTS[1],
    { ...EVENTS[1], seq: 99, ticker: "ABBV", remark: "ABBV is going nowhere.", payload: {
      direction: "neutral", confidence: 0.4, held: true, horizon_days: 5, proposed_stop: null,
      invalidation: "A breakout on volume.", evidence: [] } },
  ];
  const room = meetingRoom({ roster: ROSTER });
  room.load({ ...MEETING, events: twice });
  room.setMarket(MARKET);
  room.seek(0);
  assert.match(words(panel(room)), /TMO 看多 0\.80/);
  assert.match(words(panel(room)), /A close below the 200-day average\./);
  assert.doesNotMatch(words(panel(room)), /breakout|ABBV|中性/);
  room.seek(1);
  assert.match(words(panel(room)), /ABBV 中性 0\.400\.40期限 5 个交易日什么情况下作废（原话）A breakout on volume\./);
  assert.doesNotMatch(words(panel(room)), /200-day|建议止损|引用的数据/);       // no stop and nothing cited this time
});

test("the panel counts only the lines played so far, and gives the whole session's tally once the record is over", () => {
  const room = roomAt(-1);
  pick(room, "trader");
  const facts = () => words(panel(room));
  for (let line = -1; line < EVENTS.length - 1; line += 1) {
    room.seek(line);
    assert.match(facts(), /到这一句为止的发言次数0 次/, `line ${line}`);          // the trader speaks only in the last line
    assert.doesNotMatch(facts(), /整场合计|下单 1 张/, `line ${line}`);
  }
  room.seek(EVENTS.length - 1);
  assert.match(facts(), /到这一句为止的发言次数1 次整场合计成交 0 笔，下单 1 张/);
  pick(room, "trader");
  pick(room, "ticker_analyst");
  room.seek(0);
  assert.match(facts(), /到这一句为止的发言次数0 次/);
  assert.doesNotMatch(facts(), /1 条观点/);
  room.seek(1);
  assert.match(facts(), /到这一句为止的发言次数1 次/);
  assert.doesNotMatch(facts(), /整场合计|1 条观点/);                              // one line in: the day's tally is still to come

  // A run that is still going has no final tally to give, even at its latest line.
  const going = meetingRoom({ roster: ROSTER });
  going.load({ ...MEETING, run: { ...MEETING.run, status: "running", live: true } });
  going.setMarket(MARKET);
  going.seek(EVENTS.length - 1);
  assert.match(words(panel(going)), /到这一句为止的发言次数1 次/);
  assert.doesNotMatch(words(panel(going)), /整场合计/);
  // The evaluation's record is dated, and says it is counted over everything up to that date.
  const scored = meetingRoom({ roster: ROSTER });
  scored.load({ ...MEETING, records: { as_of: "2026-11-20", lines: { ticker_analyst: "写过 31 条观点。" } } });
  scored.seek(1);
  assert.match(words(panel(scored)), /历史成绩（2026-11-20 的评估，按到那天为止的全部记录算）写过 31 条观点。/);
});

test("each figure in the room carries the mark of its field beside its name", () => {
  const room = roomAt(-1);
  const figures = all(room.node, (node) => node.tag === "g" && "id" in node.dataset);
  assert.equal(figures.length, ROSTER.length);
  for (const drawn of figures) {
    const marks = drawn.children.filter((part) => part.attributes.class === "emblem");
    assert.equal(marks.length, 1, drawn.dataset.id);
    assert.ok(marks[0].children.length >= 1 && marks[0].children.every((line) => line.tag === "path"));
  }
});
