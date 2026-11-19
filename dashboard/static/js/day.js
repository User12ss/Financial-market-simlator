// One trading day at the desk, above the meeting room: the meetings held that
// day, in order, and what the watch between them last saw. Everything here is
// a recorded time, a recorded count or a fixed word; nothing a model said
// passes through. The rules are plain functions, so they can be tested with
// no page in sight.

import { el, clear, word } from "./dom.js";

const two = (part) => String(part).padStart(2, "0");

/** A recorded moment as an hour and a minute, in this computer's own time zone. */
export function minute(iso) {
  const moment = new Date(iso || NaN);
  return Number.isNaN(moment.getTime()) ? "—" : `${two(moment.getHours())}:${two(moment.getMinutes())}`;
}

/** How long before `now` (milliseconds) a recorded moment was. */
export function ago(iso, now) {
  const minutes = Math.floor((now - new Date(iso).getTime()) / 60000);
  if (!Number.isFinite(minutes)) return "—";
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 60 / 24)} 天前`;
}

const moved = (value) => `${value > 0 ? "▲ +" : value < 0 ? "▼ −" : ""}${Math.abs(value).toFixed(2)}%`;

/** How a meeting stands, in a word or two, for its place on the day's strip. */
function standing(meeting) {
  if (meeting.live) return "进行中";
  if (meeting.status === "failed") return "失败";
  if (meeting.status === "interrupted") return "中断";
  return `${meeting.events} 条${meeting.status === "degraded" ? "，有缺漏" : ""}`;
}

/** One entry per meeting of the day, in the order they were held. `shown` is the one in the room. */
export function dayChips(meetings, shown) {
  return meetings.map((meeting) => ({
    id: meeting.run_id,
    text: `${minute(meeting.started_at)} ${word("meeting", meeting.kind)}`,
    note: standing(meeting),
    pressed: meeting.run_id === shown,
    live: Boolean(meeting.live),
  }));
}

/**
 * The watch's last round in one line: when it was, what it fetched, the largest move of the day, what it could
 * not fetch, and the next meeting on the timetable if that is still ahead. Null when no round is on record
 * (a model configuration with no desk has none).
 */
export function watchText(watch, now) {
  if (!watch) return null;
  const headlines = watch.headlines || {}, quotes = watch.quotes || {};
  const parts = [
    `最近一次巡检 ${minute(watch.at)}（${ago(watch.at, now)}）`,
    `盯着 ${watch.tickers} 只股票，新消息 ${headlines.added || 0} 条，报价 ${quotes.count || 0} 个`,
  ];
  const biggest = (watch.moves || [])[0];
  if (biggest) parts.push(`当天波动最大 ${biggest.ticker} ${moved(biggest.change_pct)}`);
  const missing = [...(headlines.failed || []).map((ticker) => `${ticker} 的新闻`), ...(quotes.error ? ["报价"] : [])];
  if (missing.length) parts.push(`没取到：${missing.join("、")}`);
  const next = watch.next_meeting;
  if (next && new Date(next.at).getTime() > now) parts.push(`下一场 ${minute(next.at)} ${word("meeting", next.kind)}`);
  return parts.join(" · ");
}

/**
 * The strip itself. `onPick(day, id)` is called when a meeting is chosen; `onNow()`, when given, when the
 * reader asks to go back to what the desk is at now.
 */
export function dayStrip({ onPick, onNow = null, now = () => Date.now() }) {
  const title = el("strong", { class: "day-title" });
  const chips = el("div", { class: "day-meetings" });
  const back = el("button", { type: "button", class: "day-now", hidden: true, text: "回到当下", onclick: () => onNow && onNow() });
  const watchLine = el("div", { class: "day-watch small" });
  const node = el("section", { class: "day", "aria-label": "这一天的会议", hidden: true },
    el("div", { class: "day-row" }, title, chips, back), watchLine);
  let state = { data: null, shown: null, away: false };

  function paint() {
    const { data, shown } = state;
    node.hidden = !data || !data.day;
    if (node.hidden) return;
    title.textContent = `${data.day} 的会议`;
    clear(chips);
    if (data.meetings.length === 0) chips.append(el("span", { class: "muted", text: "这一天还没有开过会。" }));
    for (const chip of dayChips(data.meetings, shown)) {
      chips.append(el("button", {
        type: "button", class: chip.live ? "day-chip live" : "day-chip", "aria-pressed": String(chip.pressed),
        onclick: () => onPick(data.day, chip.id),
      }, chip.text, el("span", { class: "small muted", text: ` ${chip.note}` })));
    }
    back.hidden = !(onNow && state.away);
    const text = watchText(data.watch, now());
    watchLine.hidden = text === null;
    watchLine.textContent = text || "";
  }

  return {
    node,
    /** `data` is the server's answer for a day; `shown` the meeting in the room; `away` whether the room has left "now". */
    load(data, shown, away = false) {
      state = { data, shown, away };
      paint();
    },
    repaint: paint,
  };
}
