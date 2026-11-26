// The page in English. The language is read once, when the page loads, from what the browser kept: this file
// stands in for a browser that kept "en", so everything it loads speaks English. (Every other test file has no
// such browser, and tests the page as it was written, in Chinese.) Run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { everything, install, wearing, words } from "./fake_dom.mjs";
import { MARKET } from "./market_fixture.mjs";

const kept = { "dashboard.lang": "en" };
globalThis.localStorage = { getItem: (name) => kept[name] ?? null, setItem: (name, value) => { kept[name] = value; } };
const { window } = install();
const { EN } = await import("../../dashboard/static/js/en.js");
const { LANG, english, fromServer, localize, setLang, t } = await import("../../dashboard/static/js/i18n.js");
const { word, span } = await import("../../dashboard/static/js/dom.js");
const { meetingRoom } = await import("../../dashboard/static/js/meeting.js");
const { shortTitle } = await import("../../dashboard/static/js/network.js");
const { shares } = await import("../../dashboard/static/js/screen.js");
const { outcomeText } = await import("../../dashboard/static/js/decisions.js");

const HAN = /[㐀-鿿]/;

test("a string of the page's own is looked up by its Chinese, a template by its fixed parts", () => {
  assert.deepEqual([LANG, english], ["en", true]);
  assert.equal(t("会议室"), "Meeting room");
  const count = 70;
  assert.equal(t`，共 ${count} 条记录`, ", 70 records");
  // Values go where the English puts them, which need not be the order the Chinese had them in.
  assert.equal(t`${"多方"}对${"空方"}说了 ${3} 句`, "多方 said 3 line(s) to 空方");
  // With no English on record the Chinese stands, whole: never a blank, never half a sentence.
  assert.equal(t("这句话没有英文"), "这句话没有英文");
  assert.equal(t`没有英文的 ${1} 句`, "没有英文的 1 句");
  // A value is written as a template would write it, a missing one included.
  assert.equal(t`${null} 收盘`, "null close");
  // The page's fixed vocabulary goes through the same table.
  assert.deepEqual([word("action", "buy"), word("direction", "bearish"), word("meeting", "close"), word("run", "ok")], ["buy", "bearish", "Close meeting", "Completed"]);
  assert.equal(span(3725), "1 h 2 min");
  assert.equal(outcomeText({ kind: "ordered", side: "buy", shares: 10 }), "Order placed: buy 10 shares");
  assert.equal(outcomeText({ kind: "ruled", stance: "own" }, true), "Judge ruled: hold, no further action");
  assert.deepEqual([shares(3_544_000), shares(1_230_000_000), shares(950)], ["3.54M", "1.23B", "950"]);
});

test("every English entry keeps the places of its key, and none is empty", () => {
  for (const [key, text] of Object.entries(EN)) {
    assert.ok(typeof text === "string" && text.length > 0, `no English for ${key}`);
    const holes = key.split("{}").length - 1;
    for (const [, place] of text.matchAll(/\{(\d+)\}/g)) assert.ok(Number(place) < holes, `${key} has no value ${place}`);
  }
  assert.ok(Object.keys(EN).length > 800);
});

test("what the server sends is put into English only where it is the server's own Chinese", () => {
  assert.equal(fromServer("组合经理"), "Portfolio Manager");
  assert.equal(fromServer("3 条观点：看多 2，中性 1，看空 0"), "3 views: bullish 2, neutral 1, bearish 0");
  assert.equal(fromServer("立场：偏空，把握度 0.80"), "Stance: restrictive, confidence 0.80");
  assert.equal(fromServer("提案：接受 2，拒绝 1；订单：批准 0，削减 2，拒绝 0"), "Proposals: accepted 2, rejected 1; orders: approved 0, reduced 2, rejected 0");
  assert.equal(fromServer("SPY 买入持有"), "SPY buy-and-hold");
  // Recorded words, ids and numbers are never touched; neither is Chinese the page has no English for.
  for (const untouched of ["Buy AAPL is accepted.", "portfolio_manager", "2026-11-25", "这是一句没有登记的话"]) assert.equal(fromServer(untouched), untouched);
  const answer = { roster: [{ id: "bull", title: "多方", group: "辩论", craft: ["上行逻辑", "催化剂"], seat: 3, kind: "llm" }],
    events: [{ speaker: "bull", speaker_title: "多方", to_title: "空方", stage_title: "辩论", remark: "Orders are up.", payload: { round: 1, held: true, note: null } }] };
  assert.deepEqual(localize(answer), { roster: [{ id: "bull", title: "Bull", group: "Debate", craft: ["Upside case", "Catalysts"], seat: 3, kind: "llm" }],
    events: [{ speaker: "bull", speaker_title: "Bull", to_title: "Bear", stage_title: "Debate", remark: "Orders are up.", payload: { round: 1, held: true, note: null } }] });
  assert.equal(shortTitle("Commodities & Inflation"), "Cmdty");
  assert.equal(shortTitle("Bull"), "Bull");
});

test("changing the language is remembered and loads the page again", () => {
  let loaded = 0;
  window.location.reload = () => { loaded += 1; };
  assert.equal(setLang("zh"), true);
  assert.deepEqual([kept["dashboard.lang"], loaded], ["zh", 1]);
  setLang("en");
  assert.deepEqual([kept["dashboard.lang"], loaded], ["en", 2]);
  setLang("something else");                                   // anything but English is the page as it was written
  assert.equal(kept["dashboard.lang"], "zh");
  kept["dashboard.lang"] = "en";
});

test("in English the whole room speaks English, and what was recorded is still word for word what it was", () => {
  const figure = (id, title, seat, group, more = {}) => ({
    id, title, kind: "llm", group, seat, does: "看收益率曲线和信用利差。", tone: seat % 7, hair: "short", extra: "none",
    emblem: "pulse", craft: ["收益率曲线", "期限利差", "信用利差"], reads: [], ...more,
  });
  const roster = localize([
    figure("portfolio_manager", "组合经理", 0, "决策"), figure("ticker_analyst", "标的分析师", 1, "标的组"), figure("bull", "多方", 2, "辩论"),
    figure("bear", "空方", 3, "辩论"), figure("judge", "裁判", 4, "辩论"), figure("risk_officer", "风控官", 5, "执行", { kind: "rule" }),
    figure("trader", "交易员", 6, "执行", { kind: "rule" }),
  ]);
  const titles = { portfolio_manager: "组合经理", ticker_analyst: "标的分析师", bull: "多方", bear: "空方", judge: "裁判", risk_officer: "风控官", trader: "交易员", quant_engineer: "量化工程师", all: "所有人" };
  const stages = { views: "观点", forecast: "预测", debate: "辩论", proposals: "提案", checks: "检查", risk: "风控", orders: "下单" };
  let seq = 0;
  const said = (stage, speaker, to, kind, ticker, remark, payload = {}) => ({
    seq: seq += 1, ts: "2026-11-12T23:00:07Z", stage, stage_title: stages[stage], speaker, speaker_title: titles[speaker], to, to_title: titles[to],
    is_llm: !["risk_officer", "trader", "quant_engineer"].includes(speaker), kind, ticker, remark, payload,
  });
  const events = [
    said("views", "ticker_analyst", "portfolio_manager", "view", "TMO", "TMO looks ready to turn.", { direction: "bullish", confidence: 0.8, held: false, horizon_days: 10, invalidation: "A close below the 200-day average.", evidence: [{ metric: "rsi14", value: 44.44, actual: 44.44, matches: true }] }),
    said("forecast", "quant_engineer", "all", "market_state", null, "Calm 0.28, turbulent 0.72.", { states: ["calm", "turbulent"], now: [0.28, 0.72] }),
    said("debate", "bull", "bear", "argument", "TMO", "Orders are up.", { side: "bull", round: 1, strongest_evidence: "Backlog grew 12%." }),
    said("debate", "bear", "bull", "argument", "TMO", "Margins are thin.", { side: "bear", round: 1 }),
    said("debate", "judge", "portfolio_manager", "ruling", "TMO", "Own it.", { stance: "own", confidence: 0.7, reasoning: "The backlog point stands." }),
    said("proposals", "portfolio_manager", "risk_officer", "proposals", null, "One buy.", { proposals: [{ ticker: "TMO", action: "buy", confidence: 0.8 }], summary: "Keep it small." }),
    said("checks", "risk_officer", "trader", "accepted", "TMO", "Buy TMO is accepted.", { action: "buy", source: "portfolio_manager" }),
    said("risk", "risk_officer", "trader", "risk_reduced", "TMO", "Reduced: buy TMO, 20 shares cut to 10.", { request: { side: "buy", shares: 20 }, decision: { side: "buy", verdict: "reduced", requested_shares: 20, approved_shares: 10 } }),
    said("orders", "trader", "all", "order_placed", "TMO", "Placed: buy 10 TMO.", { side: "buy", shares: 10 }),
  ];
  const meeting = localize({
    arm: "fake/a", session: "2026-11-12", stands: true, today: { ticker_analyst: "1 条观点：看多 1，中性 0，看空 0", trader: "成交 0 笔，下单 1 张" },
    records: { as_of: null, lines: {} },
    run: { run_id: "run-1", status: "ok", kind: "close", live: false, started_at: "2026-11-12T23:00:00Z", seconds: 65, error: null, as_of: "2026-11-12T22:53:00Z" }, events,
  });
  const room = meetingRoom({ roster });
  room.load(meeting);
  room.setMarket(MARKET);
  for (let line = -1; line < events.length; line += 1) {
    room.seek(line);
    const left = everything(room.node).filter((text) => HAN.test(String(text)));
    assert.deepEqual(left, [], `line ${line}`);
  }
  // A few things a reader sees, to hold the wording.
  room.seek(4);
  assert.match(words(wearing(room.node, "brief-head")[0]), /^The team's conclusion for you/);
  assert.deepEqual(wearing(room.node, "rail-step").map(words), ["Views1", "Forecast1", "Debate3/3", "Proposals1", "Checks1", "Risk gate1", "Orders1"]);
  assert.equal(words(wearing(room.node, "who-name")[0]), "Judge to Portfolio Manager");
  assert.match(words(wearing(room.node, "debate")[0]), /^Debate, round by round · TMO/);
  assert.equal(words(wearing(room.node, "debate-ruling")[0]), "Judge▲ Ruled: holdConfidence 0.70Own it.");
  assert.equal(words(wearing(room.node, "lead")[0]), "LEAD DESK · YOU");
  assert.deepEqual(wearing(room.node, "out").map(words), ["▲TMOOrder placed: buy 10 shares"]);
  // The recorded words themselves: exactly as recorded.
  assert.equal(words(wearing(room.node, "words")[0]), "Own it.Own it.");
  assert.ok(everything(room.node).includes("Reduced: buy TMO, 20 shares cut to 10."));
});
