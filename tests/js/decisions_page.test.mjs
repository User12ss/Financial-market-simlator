// The 决策 page: the grid of sessions and the trail under it are read from the same run, and every link on it
// leads to that run. Run with `node --test`. There is no browser and no server here: both are stand-ins.

import assert from "node:assert/strict";
import { test } from "node:test";

import { all, install, words as onShow } from "./fake_dom.mjs";

const { window } = install();
const { decisionsPage, roomPage } = await import("../../dashboard/static/js/pages.js");

const cell = (fields) => ({
  direction: null, confidence: null, held: false, action: null, source: null, check: null, repeats: 0,
  kept_by_default: false, verdict: null, approved_shares: null, ordered: null, ...fields,
});
let seq = 0;
const line = (stage, speaker, kind, ticker, payload = {}) => ({
  seq: seq += 1, ts: "2025-11-14T21:00:00+00:00", stage, stage_title: stage, speaker, speaker_title: speaker, to: "all",
  to_title: "所有人", is_llm: true, kind, ticker, payload, remark: `${speaker} on ${ticker || "the day"}`,
});

// Two finished sessions. The first run's name has a space and a slash in it, as a name might.
const HISTORY = {
  arm: "fake/a", tickers: ["UPA", "UPB"],
  sessions: [
    {
      session: "2025-11-13", run_id: "run 13/a", status: "ok", halted: false, regime: { risk_appetite: "risk_on", confidence: 0.7 },
      tickers: {
        UPA: cell({
          direction: "bullish", confidence: 0.8, action: "buy", source: "portfolio_manager", check: "accepted",
          verdict: "approved", approved_shares: 10, ordered: { side: "buy", shares: 10 },
        }),
        UPB: cell({ direction: "bullish", confidence: 0.6 }),
      },
    },
    {
      session: "2025-11-14", run_id: "run-14-b", status: "ok", halted: false, regime: null,
      tickers: {
        UPA: cell({ direction: "bearish", confidence: 0.55, held: true, action: "hold", source: "portfolio_manager", check: "accepted", repeats: 1 }),
      },
    },
  ],
};
const RECORDS = {
  "run 13/a": [
    line("views", "ticker_analyst", "view", "UPA", { direction: "bullish", confidence: 0.8, held: false }),
    line("views", "ticker_analyst", "view", "UPB", { direction: "bullish", confidence: 0.6, held: false }),
    line("proposals", "portfolio_manager", "proposals", null, { proposals: [{ ticker: "UPA", action: "buy", confidence: 0.7 }] }),
    line("checks", "risk_officer", "accepted", "UPA", { action: "buy", source: "portfolio_manager" }),
  ],
  "run-14-b": [
    line("macro", "central_bank", "assessment", null, { stance: "neutral", confidence: 0.5 }),
    line("views", "ticker_analyst", "view", "UPA", { direction: "bearish", confidence: 0.55, held: true }),
    line("proposals", "portfolio_manager", "proposals", null, { proposals: [
      { ticker: "UPA", action: "hold", confidence: 0.5 }, { ticker: "UPA", action: "sell", confidence: 0.7 }] }),
    line("checks", "risk_officer", "accepted", "UPA", { action: "hold", source: "portfolio_manager" }),
    line("checks", "risk_officer", "rejected", "UPA", { action: "sell", source: "portfolio_manager" }),
  ],
};

const asked = [];
const slow = new Set();                    // runs whose record the server is slow to send
const waiting = new Map();                 // those that were asked for: run → a way to let the answer go
globalThis.fetch = async (address, how) => {
  assert.deepEqual(how, { method: "GET", cache: "no-store" });
  asked.push(address);
  const { pathname, searchParams } = new URL(address, "http://127.0.0.1:8765");
  // A run asked for by name; with no name, the latest session's.
  const run = searchParams.has("run_id") ? searchParams.get("run_id") : "run-14-b";
  if (pathname === "/api/market" && down.has(run)) return { ok: false, status: 500, json: async () => ({ error: "账本读不了" }) };
  const data = pathname === "/api/history" ? HISTORY
    : pathname === "/api/day" ? {
      arm: "fake/a", day: searchParams.get("day"), watch: null,
      meetings: [{ run_id: "run-14-b", kind: "close", status: "ok", live: false, started_at: "2025-11-14T21:00:00", events: (RECORDS["run-14-b"] || []).length }],
    }
    : pathname === "/api/market" ? { available: run in RECORDS, reason: run in RECORDS ? null : "no_run", run_id: run, session: "2025-11-14", tickers: {}, market: [], macro: [] }
      : {
        arm: "fake/a", session: "2025-11-14", stands: true, today: {}, records: { as_of: null, lines: {} },
        run: run in RECORDS ? { run_id: run, status: "ok", live: false, started_at: "2025-11-14T21:00:00+00:00", seconds: 3, error: null } : null,
        events: RECORDS[run] || [],
      };
  if (slow.has(run)) await new Promise((release) => waiting.set(run, release));
  return { ok: true, status: 200, json: async () => data };
};
const down = new Set();                    // runs whose market data the server fails to give
/** Let everything the page has asked for come back and be shown. */
const settled = async () => { for (let turn = 0; turn < 5; turn += 1) await new Promise((done) => setImmediate(done)); };

const links = (page) => all(page.node, (node) => node.tag === "a");
const days = (page) => all(page.node, (node) => node.tag === "button" && "data-day" in node.attributes);
const cells = (page) => all(page.node, (node) => node.tag === "button" && node.attributes.class === "node" && !node.hidden);
const words = (node) => node.textContent + node.children.map(words).join("");
const said = (node) => node.children.map(words);
const results = (page) => all(page.node, (node) => node.attributes.class === "trail-result").map((node) => node.textContent);

test("the grid and the trail under it are read from the same run, and every link leads to that run", async () => {
  const page = decisionsPage({ arm: "fake/a" });
  await settled();
  // The trail is asked for by the name of the run the latest column was read from, not by its day.
  assert.deepEqual(asked, ["/api/history?arm=fake%2Fa", "/api/meeting?arm=fake%2Fa&run_id=run-14-b"]);

  // Each cell of the grid leads to the replay of its own column's run.
  assert.deepEqual(links(page).map((link) => link.attributes.href), [
    "#replay/2025-11-13/run%2013%2Fa", "#replay/2025-11-14/run-14-b", "#replay/2025-11-13/run%2013%2Fa",
  ]);
  const [bought, kept] = links(page);
  assert.deepEqual(said(bought), ["▲ 看多 0.80", "买入 10 股，风控放行"]);
  assert.deepEqual(said(kept), ["▼ 看空 0.55", "持有", "另有 1 条重复提案不算", "当时持仓", "上一场是看多"]);
  assert.deepEqual([bought.attributes.class, kept.attributes.class], ["cell-box", "cell-box held changed"]);

  // The newest day is the one picked, and its trail is shown whole: where each decision ended.
  assert.deepEqual(days(page).map((day) => [day.textContent, day.attributes["aria-pressed"]]),
    [["2025-11-14", "true"], ["2025-11-13", "false"]]);
  assert.deepEqual(results(page), ["继续持有，不动"]);
  assert.deepEqual(all(page.node, (node) => node.textContent === "直接看最终结果"), []);      // nothing to choose here
  assert.deepEqual(cells(page).map((node) => node.dataset.state), ["done", "done", "done"]);
  // A cell of the trail leads to its line in the replay of that same run (lines are counted from one there).
  cells(page).find((node) => node.attributes["aria-label"] === "UPA 检查：接受").listeners.click();
  assert.equal(window.location.hash, "#replay/2025-11-14/run-14-b/4");

  // Picking the other day reads that day's run, by name, and its cells lead there.
  days(page)[1].listeners.click();
  await settled();
  assert.equal(asked[asked.length - 1], "/api/meeting?arm=fake%2Fa&run_id=run+13%2Fa");
  assert.deepEqual(days(page).map((day) => day.attributes["aria-pressed"]), ["false", "true"]);
  assert.deepEqual(results(page), ["买入的提案通过检查，之后没有进一步动作", "分析师看多，之后没有进一步动作"]);
  cells(page).find((node) => node.attributes["aria-label"] === "UPA 提案：买入 0.70").listeners.click();
  assert.equal(window.location.hash, "#replay/2025-11-13/run%2013%2Fa/3");

  // Looking again keeps the day that was picked, and still reads it from the run its column stands for.
  await page.refresh();
  await settled();
  assert.deepEqual(asked.slice(-2), ["/api/history?arm=fake%2Fa", "/api/meeting?arm=fake%2Fa&run_id=run+13%2Fa"]);
  assert.deepEqual(days(page).map((day) => day.attributes["aria-pressed"]), ["false", "true"]);
  assert.equal(asked.some((address) => address.includes("session=")), false);
});

test("a day that has been run again since is read from the run that stands for it now", async () => {
  const page = decisionsPage({ arm: "fake/a" });
  await settled();
  days(page)[1].listeners.click();                               // the 13th
  await settled();
  const before = HISTORY.sessions[0];
  HISTORY.sessions[0] = { ...before, run_id: "run-13-again" };
  RECORDS["run-13-again"] = RECORDS["run 13/a"].slice(0, 2);
  try {
    await page.refresh();
    await settled();
    assert.equal(asked[asked.length - 1], "/api/meeting?arm=fake%2Fa&run_id=run-13-again");
    assert.equal(links(page)[0].attributes.href, "#replay/2025-11-13/run-13-again");
    assert.deepEqual(results(page), ["分析师看多，之后没有进一步动作", "分析师看多，之后没有进一步动作"]);
    cells(page)[0].listeners.click();
    assert.equal(window.location.hash, "#replay/2025-11-13/run-13-again/1");
  } finally {
    HISTORY.sessions[0] = before;
  }
});

test("an answer that comes back after another day was picked is not shown", async () => {
  const page = decisionsPage({ arm: "fake/a" });
  await settled();
  slow.add("run 13/a");
  try {
    days(page)[1].listeners.click();                             // the 13th: its record is slow to come
    await settled();
    days(page)[0].listeners.click();                             // back to the 14th before it has
    await settled();
    assert.deepEqual(results(page), ["继续持有，不动"]);
    waiting.get("run 13/a")();                                   // now the 13th's record arrives
    await settled();
    assert.deepEqual(results(page), ["继续持有，不动"]);          // the day that is picked is still the one shown
    assert.deepEqual(days(page).map((day) => day.attributes["aria-pressed"]), ["true", "false"]);
  } finally {
    slow.clear();
  }
});

test("a run the server no longer has leaves the page as it was", async () => {
  const before = HISTORY.sessions[0];
  HISTORY.sessions[0] = { ...before, run_id: "gone" };
  try {
    const page = decisionsPage({ arm: "fake/a" });
    await settled();
    days(page)[1].listeners.click();
    await settled();
    assert.equal(asked[asked.length - 1], "/api/meeting?arm=fake%2Fa&run_id=gone");
    assert.deepEqual(results(page), ["继续持有，不动"]);          // still the 14th's trail, and nothing broke
  } finally {
    HISTORY.sessions[0] = before;
  }
});

test("a room reads the market data of the run it shows and puts that run's prices on the tape", async () => {
  const figure = (id, seat) => ({
    id, title: id, kind: "llm", group: "组", seat, does: "职责", tone: seat, hair: "short", extra: "none", emblem: "pulse",
    craft: ["一", "二", "三"], reads: [],
  });
  const roster = ["central_bank", "ticker_analyst", "portfolio_manager", "risk_officer"].map(figure);
  const room = (taped) => roomPage({ arm: "fake/a", roster, live: () => ({ run: null, events: [] }), tape: (market) => taped.push(market) });

  const taped = [];
  asked.length = 0;
  const page = room(taped);
  assert.deepEqual(taped, [null]);                               // at once: no other session's prices over this room
  await settled();
  // The market data by the run's name, and the day the run belongs to, for the strip above the room.
  assert.deepEqual(asked, ["/api/meeting?arm=fake%2Fa", "/api/market?arm=fake%2Fa&run_id=run-14-b",
    "/api/day?arm=fake%2Fa&day=2025-11-14"]);
  assert.deepEqual(taped.map((market) => market && market.run_id), [null, "run-14-b"]);
  const strip = () => all(page.node, (node) => node.attributes.class === "day")[0];
  assert.equal(onShow(strip()), "2025-11-14 的会议21:00 收盘会 5 条");
  assert.deepEqual(all(strip(), (node) => node.tag === "button" && !node.hidden).map((node) => node.attributes["aria-pressed"]), ["true"]);
  await page.refresh();                                          // nothing new: the market data is not read again
  await settled();
  assert.equal(asked.filter((address) => address.startsWith("/api/market")).length, 1);
  // The strip is read again once a minute, because the watch goes on between meetings; and no longer once
  // the page is left.
  const timer = window.intervals[window.intervals.length - 1];
  assert.deepEqual([timer.every, timer.stopped], [60000, false]);
  const before = asked.filter((address) => address.startsWith("/api/day")).length;
  timer.work();
  await settled();
  assert.equal(asked.filter((address) => address.startsWith("/api/day")).length, before + 1);
  // Choosing a meeting on the strip opens that one by name; the way back to now then appears.
  all(strip(), (node) => node.attributes.class === "day-chip")[0].listeners.click();
  await settled();
  assert.equal(asked.filter((address) => address === "/api/meeting?arm=fake%2Fa&session=2025-11-14&run_id=run-14-b").length, 1);
  const back = all(strip(), (node) => node.attributes.class === "day-now")[0];
  assert.equal(back.hidden, false);
  const latest = () => asked.filter((address) => address === "/api/meeting?arm=fake%2Fa").length;
  const asksBefore = latest();
  back.listeners.click();
  await settled();
  assert.equal(latest(), asksBefore + 1, "back to whatever the desk is at: asked for with no name");
  assert.equal(all(strip(), (node) => node.attributes.class === "day-now")[0].hidden, true);
  page.destroy();
  assert.equal(timer.stopped, true);

  // When the server cannot give the market data, the tape shows nothing and the room says so; it asks again next time.
  down.add("run-14-b");
  try {
    const dark = [];
    asked.length = 0;
    const failing = room(dark);
    await settled();
    assert.deepEqual(dark, [null, null]);
    assert.match(all(failing.node, (node) => node.attributes.class === "holo-empty").map(words).join(""), /看板服务没有给出回答/);
    down.clear();
    HISTORY.arm = "fake/a";                                      // (nothing changes in the record)
    RECORDS["run-14-b"] = [...RECORDS["run-14-b"], line("checks", "risk_officer", "accepted", "UPB", { action: "hold", source: "default" })];
    await failing.refresh();                                     // the record has grown: the room is loaded again
    await settled();
    assert.deepEqual(dark.map((market) => market && market.run_id), [null, null, "run-14-b"]);
    failing.destroy();
  } finally {
    down.clear();
    RECORDS["run-14-b"] = RECORDS["run-14-b"].slice(0, 5);
  }
});

test("with nothing finished yet the page says so and asks for no record", async () => {
  const sessions = HISTORY.sessions.splice(0);
  const tickers = HISTORY.tickers.splice(0);
  asked.length = 0;
  try {
    const page = decisionsPage({ arm: "fake/a" });
    await settled();
    assert.deepEqual(asked, ["/api/history?arm=fake%2Fa"]);
    const notes = all(page.node, (node) => node.attributes.class === "empty").map((node) => said(node).join(""));
    assert.deepEqual(notes.slice(0, 2), ["还没有完成过一场，没有可以比的。", "还没有完成过一场。"]);
    assert.deepEqual([links(page), days(page), cells(page)], [[], [], []]);
  } finally {
    HISTORY.sessions.push(...sessions);
    HISTORY.tickers.push(...tickers);
  }
});
