// Shared helpers. Nothing here builds HTML from a string: every piece of
// recorded text reaches the page through textContent, so what a model wrote
// can never be read as markup. The only request this page ever makes is GET.

import { localize, t } from "./i18n.js";

const SVG_NS = "http://www.w3.org/2000/svg";
export const DASH = "—";

function build(node, attrs, children) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) {
      // A handler is a function of this page's own. A string here would be run as script, so it is refused.
      if (typeof value !== "function") throw new TypeError(`${key} must be a function`);
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const el = (tag, attrs, ...children) => build(document.createElement(tag), attrs, children);
export const svg = (tag, attrs, ...children) => build(document.createElementNS(SVG_NS, tag), attrs, children);

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Replace what a node holds. Children that are null or false are left out, and text stays text, as in `el`. */
export const fill = (node, ...children) => build(clear(node), {}, children);

// ── Reading from the server ──────────────────────────────────────────────────

export async function getJSON(path, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") query.set(key, value);
  }
  const address = query.size ? `${path}?${query}` : path;
  try {
    const response = await fetch(address, { method: "GET", cache: "no-store" });
    // In English the server's own names and tallies are put into English here, once, for every page.
    return { ok: response.ok, status: response.status, data: localize(await response.json()) };
  } catch (error) {
    return { ok: false, status: 0, data: { error: t("连不上看板服务。它可能已经停了。") } };
  }
}

// ── Numbers and dates ────────────────────────────────────────────────────────

const fixed = (value, digits) =>
  Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const missing = (value) => value === null || value === undefined || Number.isNaN(value);

export function usd(value, digits = 2) {
  if (missing(value)) return DASH;
  return `${value < 0 ? "−" : ""}$${fixed(value, digits)}`;
}

// Zero carries no sign: "+$0.00" would claim a gain that is not there.
const sign = (value) => (value < 0 ? "−" : value > 0 ? "+" : "");

export function signedUsd(value, digits = 2) {
  if (missing(value)) return DASH;
  return `${sign(value)}$${fixed(value, digits)}`;
}

export function pct(value, digits = 2) {
  if (missing(value)) return DASH;
  return `${value < 0 ? "−" : ""}${fixed(value, digits)}%`;
}

export function signedPct(value, digits = 2) {
  if (missing(value)) return DASH;
  return `${sign(value)}${fixed(value, digits)}%`;
}

/** A rate kept as a fraction (0.62), shown as a percentage (62%). */
export function rate(value, digits = 0) {
  return missing(value) ? DASH : pct(value * 100, digits);
}

export function num(value, digits = 0) {
  if (missing(value)) return DASH;
  return `${value < 0 ? "−" : ""}${fixed(value, digits)}`;
}

/** A recorded moment, in this computer's own time zone. */
export function when(iso) {
  if (!iso) return DASH;
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return iso;
  const two = (part) => String(part).padStart(2, "0");
  return `${two(moment.getMonth() + 1)}-${two(moment.getDate())} ${two(moment.getHours())}:${two(moment.getMinutes())}`;
}

export function clock(iso) {
  if (!iso) return DASH;
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return iso;
  const two = (part) => String(part).padStart(2, "0");
  return `${two(moment.getHours())}:${two(moment.getMinutes())}:${two(moment.getSeconds())}`;
}

export function span(seconds) {
  if (missing(seconds)) return DASH;
  const whole = Math.round(seconds);
  if (whole < 60) return t`${whole} 秒`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return t`${minutes} 分 ${whole % 60} 秒`;
  return t`${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

export function bytes(count) {
  if (missing(count)) return DASH;
  if (count < 1024) return `${count} B`;
  if (count < 1024 * 1024) return `${(count / 1024).toFixed(1)} KB`;
  return `${(count / 1024 / 1024).toFixed(1)} MB`;
}

// ── Labels for recorded field values ─────────────────────────────────────────
// These translate fixed vocabulary (a side, a status). Free text a model or a
// rule wrote is never passed through here: it is shown as recorded.

const WORDS = {
  side: { buy: t("买入"), sell: t("卖出") },
  kind: { market: t("市价"), limit: t("限价"), stop: t("止损") },
  direction: { bullish: t("看多"), neutral: t("中性"), bearish: t("看空") },
  action: { buy: t("买入"), sell: t("卖出"), trim: t("减仓"), hold: t("持有"), keep: t("保留"), withdraw: t("撤回买单") },
  // The desk's meetings through the day, and what an analyst's update at a check-in does to a view that stands.
  // A called one is held because a condition of a plan said to meet.
  meeting: { premarket: t("盘前会"), midday: t("午间碰头"), afternoon: t("尾盘碰头"), called: t("临时碰头"), close: t("收盘会") },
  effect: { unchanged: t("不变"), strengthened: t("加强"), weakened: t("削弱"), broken: t("不成立") },
  verdict: { approved: t("批准"), reduced: t("削减"), rejected: t("拒绝") },
  check: { accepted: t("接受"), rejected: t("拒绝") },
  run: {
    ok: t("正常完成"), degraded: t("降级完成"), failed: t("失败"), running: t("运行中"), superseded: t("已被重跑取代"),
    interrupted: t("中断（进程已不在）"),
  },
  appetite: { risk_on: t("偏进取"), neutral: t("中性"), risk_off: t("偏防守") },
  stance: { supportive: t("偏多"), neutral: t("中性"), restrictive: t("偏空") },
  ownership: { own: t("持有"), do_not_own: t("不持有") },
  tilt: { overweight: t("超配"), underweight: t("低配") },
  trigger: { top_candidate: t("头号候选"), exit_signal: t("退出信号") },
  debater: { bull: t("多方"), bear: t("空方") },
  trade: { open: t("持仓中"), closed: t("已平仓") },
  position: { candidate: t("候选"), held: t("已持有") },
  // The figures an analyst may cite, by the snapshot's own names for them.
  metric: {
    close: t("收盘价"), change_1d_pct: t("1 日涨跌%"), return_5d_pct: t("5 日涨跌%"), return_1mo_pct: t("1 月涨跌%"),
    return_3mo_pct: t("3 月涨跌%"), return_1y_pct: t("1 年涨跌%"), high_52w: t("52 周高点"), low_52w: t("52 周低点"),
    sma20: t("20 日均线"), sma50: t("50 日均线"), sma200: t("200 日均线"), rsi14: "RSI 14", macd: "MACD",
    macd_signal: t("MACD 信号线"), atr14_pct: "ATR%", trailing_pe: t("市盈率"), forward_pe: t("预期市盈率"),
    profit_margin_pct: t("利润率%"), beta: "Beta", market_cap_billion: t("市值（十亿美元）"),
  },
};

export function word(group, value) {
  if (missing(value) || value === "") return DASH;
  return (WORDS[group] || {})[value] || String(value);
}

// ── Small pieces of page ─────────────────────────────────────────────────────

/** A change with its direction said twice: by an arrow and by colour. */
export function delta(value, text) {
  if (missing(value)) return el("span", { class: "muted", text: DASH });
  // The figure is given as a child, never as the span's text: it may be a node of its own (a counting figure),
  // and a node set as text shows as its type's name. An account on its first day has made exactly nothing.
  if (value === 0) return el("span", {}, text);
  return el("span", { class: value > 0 ? "up" : "down" }, value > 0 ? "▲ " : "▼ ", text);
}

/** A state with a mark and a label, never colour alone. */
export function state(level, label) {
  return el("span", { class: `state state-${level}`, text: label });
}

export function runState(run) {
  if (!run) return state("idle", t("没有运行记录"));
  const level = {
    ok: "good", degraded: "warning", failed: "critical", interrupted: "critical", running: "live",
  }[run.status] || "idle";
  return state(level, word("run", run.status));
}

export function tile(label, value, note, hero = false) {
  return el("div", { class: hero ? "tile hero" : "tile" },
    el("div", { class: "label", text: label }),
    el("div", { class: "value" }, value),
    note ? el("div", { class: "note" }, note) : null);
}

export function card(title, sub, ...children) {
  return el("section", { class: "card" },
    el("h2", { text: title }), sub ? el("p", { class: "sub" }, sub) : null, ...children);
}

export function empty(...children) {
  return el("p", { class: "empty" }, ...children);
}

/**
 * A table. Each column: { title, num, cell(row) } where cell returns text or a node.
 * Returns the empty note instead when there are no rows.
 */
export function table(columns, rows, none = t("没有记录。")) {
  if (!rows || rows.length === 0) return empty(none);
  const head = el("tr", {}, columns.map((column) => el("th", { class: column.num ? "num" : null, text: column.title })));
  const body = rows.map((row) => el("tr", {}, columns.map((column) => {
    const value = column.cell(row);
    return el("td", { class: [column.num ? "num" : "", column.said ? "said" : ""].join(" ").trim() || null },
      missing(value) ? DASH : value);
  })));
  return el("div", { class: "scroll" }, el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
}

// ── The one tooltip ──────────────────────────────────────────────────────────

const tip = () => document.getElementById("tooltip");

/** rows: [{ color, value, name, plain }]. The value leads; the name follows. A plain row is a sentence, not a figure. */
export function showTip(x, y, head, rows) {
  const box = tip();
  clear(box);
  if (head) box.append(el("div", { class: "head", text: head }));
  for (const row of rows) {
    box.append(el("div", { class: "line" },
      row.color ? el("span", { class: "stroke", style: `background:${row.color}` }) : null,
      el("span", { class: row.plain ? "v plain" : "v", text: row.value }),
      el("span", { class: "n", text: row.name })));
  }
  box.hidden = false;
  const size = box.getBoundingClientRect();
  const left = Math.min(x + 14, window.innerWidth - size.width - 8);
  const top = y + 14 + size.height > window.innerHeight ? y - size.height - 10 : y + 14;
  box.style.left = `${Math.max(8, left)}px`;
  box.style.top = `${Math.max(8, top)}px`;
}

export function hideTip() {
  tip().hidden = true;
}
