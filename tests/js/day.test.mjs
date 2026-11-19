// The strip above the meeting room: the day's meetings in order and the watch's last round. Run with
// `node --test`. There is no browser here, so the page's pieces are stand-ins.

import assert from "node:assert/strict";
import { test } from "node:test";

import { all, install, words } from "./fake_dom.mjs";

install();
const { ago, dayChips, dayStrip, minute, watchText } = await import("../../dashboard/static/js/day.js");

// Moments are written without a zone, so they are read in whatever zone the test runs in, as the page reads them.
const at = (clock) => `2026-11-17T${clock}:00`;
const ms = (clock) => new Date(at(clock)).getTime();
const meeting = (kind, started, extra = {}) =>
  ({ run_id: `2026-11-17_${kind}`, kind, started_at: at(started), status: "ok", live: false, events: 5, ...extra });
const DAY = {
  arm: "desk/fake/a", day: "2026-11-17",
  meetings: [
    meeting("premarket", "08:30", { events: 1 }),
    meeting("midday", "12:00", { status: "degraded", events: 9 }),
    meeting("afternoon", "15:00", { status: "running", live: true, events: 3 }),
  ],
  watch: {
    at: at("14:45"), day: "2026-11-17", tickers: 6,
    headlines: { added: 2, failed: [], new: [] }, quotes: { count: 8, error: "" },
    moves: [{ ticker: "KO", last: 96.38, change_pct: -1.74 }, { ticker: "DIS", last: 119.2, change_pct: 0.59 }],
    market: [], next_meeting: { kind: "afternoon", at: at("15:00") },
  },
};

test("a recorded moment is shown as an hour and a minute, and how long ago it was in whole units", () => {
  assert.equal(minute(at("08:05")), "08:05");
  assert.equal(minute(null), "—");
  assert.equal(minute("not a moment"), "—");
  assert.deepEqual(["14:45", "14:44", "14:15", "13:46", "13:45", "11:00"].map((clock) => ago(at(clock), ms("14:45"))),
    ["刚刚", "1 分钟前", "30 分钟前", "59 分钟前", "1 小时前", "3 小时前"]);
  assert.equal(ago("2026-11-16T14:46:00", ms("14:45")), "23 小时前");
  assert.equal(ago("2026-11-16T14:44:00", ms("14:45")), "1 天前");
  assert.equal(ago("2026-11-15T14:44:00", ms("14:45")), "2 天前");
  assert.equal(ago("not a moment", ms("14:45")), "—");
});

test("the day's meetings are listed in the order they were held, each with how it stands", () => {
  assert.deepEqual(dayChips(DAY.meetings, "2026-11-17_midday"), [
    { id: "2026-11-17_premarket", text: "08:30 盘前会", note: "1 条", pressed: false, live: false },
    { id: "2026-11-17_midday", text: "12:00 午间碰头", note: "9 条，有缺漏", pressed: true, live: false },
    { id: "2026-11-17_afternoon", text: "15:00 尾盘碰头", note: "进行中", pressed: false, live: true },
  ]);
  const others = dayChips([
    meeting("close", "18:30", { status: "failed", events: 0 }), meeting("midday", "12:00", { status: "interrupted" }),
  ], null);
  assert.deepEqual(others.map((chip) => [chip.text, chip.note, chip.pressed]),
    [["18:30 收盘会", "失败", false], ["12:00 午间碰头", "中断", false]]);
  assert.deepEqual(dayChips([], "x"), []);
});

test("the watch's last round is said in one line, with what it could not fetch", () => {
  assert.equal(watchText(DAY.watch, ms("14:52")),
    "最近一次巡检 14:45（7 分钟前） · 盯着 6 只股票，新消息 2 条，报价 8 个 · 当天波动最大 KO ▼ −1.74% · 下一场 15:00 尾盘碰头");
  const gaps = { ...DAY.watch, headlines: { added: 0, failed: ["KO", "MA"] }, quotes: { count: 0, error: "outage" }, moves: [] };
  assert.equal(watchText(gaps, ms("15:10")),
    "最近一次巡检 14:45（25 分钟前） · 盯着 6 只股票，新消息 0 条，报价 0 个 · 没取到：KO 的新闻、MA 的新闻、报价");
  const up = { ...DAY.watch, moves: [{ ticker: "DIS", change_pct: 2 }], next_meeting: null };
  assert.equal(watchText(up, ms("14:45")),
    "最近一次巡检 14:45（刚刚） · 盯着 6 只股票，新消息 2 条，报价 8 个 · 当天波动最大 DIS ▲ +2.00%");
  assert.match(watchText({ ...DAY.watch, moves: [{ ticker: "PG", change_pct: 0 }] }, ms("14:45")), /当天波动最大 PG 0\.00%/);
});

test("a meeting on the timetable is named only while it is still ahead, and no watch means no line", () => {
  assert.match(watchText(DAY.watch, ms("14:59")), /下一场 15:00 尾盘碰头$/);
  assert.doesNotMatch(watchText(DAY.watch, ms("15:00")), /下一场/);
  assert.equal(watchText(null, ms("15:00")), null);
  assert.equal(watchText(undefined, ms("15:00")), null);
});

function strip(options = {}) {
  const picked = [], backs = [];
  const made = dayStrip({
    onPick: (day, id) => picked.push([day, id]), now: () => ms("14:52"),
    ...(options.withNow ? { onNow: () => backs.push("now") } : {}),
  });
  const buttons = () => all(made.node, (node) => node.tag === "button" && !node.hidden);
  return { ...made, picked, backs, buttons };
}

test("the strip shows the day, its meetings and the watch, and choosing a meeting names it", () => {
  const made = strip();
  assert.equal(made.node.hidden, true, "nothing is shown before a day has been read");
  made.load(DAY, "2026-11-17_midday");
  assert.equal(made.node.hidden, false);
  assert.match(words(made.node), /^2026-11-17 的会议08:30 盘前会 1 条12:00 午间碰头 9 条，有缺漏15:00 尾盘碰头 进行中最近一次巡检 14:45/);
  const buttons = made.buttons();
  assert.deepEqual(buttons.map((button) => button.attributes["aria-pressed"]), ["false", "true", "false"]);
  assert.deepEqual(buttons.map((button) => button.attributes.class), ["day-chip", "day-chip", "day-chip live"]);
  buttons[0].listeners.click();
  buttons[2].listeners.click();
  assert.deepEqual(made.picked, [["2026-11-17", "2026-11-17_premarket"], ["2026-11-17", "2026-11-17_afternoon"]]);
});

test("the way back to now is offered only where there is one, and only once the room has left it", () => {
  const without = strip();
  without.load(DAY, "2026-11-17_premarket", true);
  assert.equal(without.buttons().length, 3);

  const made = strip({ withNow: true });
  made.load(DAY, "2026-11-17_afternoon", false);
  assert.equal(made.buttons().length, 3, "the room is at now: nothing to go back to");
  made.load(DAY, "2026-11-17_premarket", true);
  const back = made.buttons()[3];
  assert.equal(words(back), "回到当下");
  back.listeners.click();
  assert.deepEqual(made.backs, ["now"]);
});

test("a day with no meeting says so, and a configuration with no desk shows no watch line", () => {
  const made = strip();
  const watchLine = () => all(made.node, (node) => node.attributes.class === "day-watch small")[0];
  made.load(DAY, null);
  assert.equal(watchLine().hidden, false);
  made.load({ arm: "fake/a", day: "2026-11-17", meetings: [], watch: null }, null);
  assert.equal(words(made.node), "2026-11-17 的会议这一天还没有开过会。");
  assert.equal(watchLine().hidden, true, "with no watch on record its line is taken away, not left empty");
  made.load({ arm: "fake/a", day: null, meetings: [], watch: null }, null);
  assert.equal(made.node.hidden, true);
  made.load({ arm: "fake/a", day: "2026-11-18", meetings: [meeting("close", "18:00", { events: 43 })], watch: null }, "x");
  assert.equal(words(made.node), "2026-11-18 的会议18:00 收盘会 43 条");
});

test("the strip is redrawn in place: an earlier day's meetings do not stay behind", () => {
  const made = strip();
  made.load(DAY, null);
  made.load({ ...DAY, meetings: DAY.meetings.slice(0, 1) }, null);
  assert.equal(made.buttons().length, 1);
  made.repaint();
  assert.equal(made.buttons().length, 1);
});
