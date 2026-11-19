// Shared helpers. Nothing here builds HTML from a string: every piece of
// recorded text reaches the page through textContent, so what a model wrote
// can never be read as markup. The only request this page ever makes is GET.

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
    return { ok: response.ok, status: response.status, data: await response.json() };
  } catch (error) {
    return { ok: false, status: 0, data: { error: "连不上看板服务。它可能已经停了。" } };
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
  if (whole < 60) return `${whole} 秒`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes} 分 ${whole % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
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
  side: { buy: "买入", sell: "卖出" },
  kind: { market: "市价", limit: "限价", stop: "止损" },
  direction: { bullish: "看多", neutral: "中性", bearish: "看空" },
  action: { buy: "买入", sell: "卖出", trim: "减仓", hold: "持有", keep: "保留", withdraw: "撤回买单" },
  // The desk's meetings through the day, and what an analyst's update at a check-in does to a view that stands.
  meeting: { premarket: "盘前会", midday: "午间碰头", afternoon: "尾盘碰头", close: "收盘会" },
  effect: { unchanged: "不变", strengthened: "加强", weakened: "削弱", broken: "不成立" },
  verdict: { approved: "批准", reduced: "削减", rejected: "拒绝" },
  check: { accepted: "接受", rejected: "拒绝" },
  run: {
    ok: "正常完成", degraded: "降级完成", failed: "失败", running: "运行中", superseded: "已被重跑取代",
    interrupted: "中断（进程已不在）",
  },
  appetite: { risk_on: "偏进取", neutral: "中性", risk_off: "偏防守" },
  stance: { supportive: "偏多", neutral: "中性", restrictive: "偏空" },
  ownership: { own: "持有", do_not_own: "不持有" },
  tilt: { overweight: "超配", underweight: "低配" },
  trigger: { top_candidate: "头号候选", exit_signal: "退出信号" },
  debater: { bull: "多方", bear: "空方" },
  trade: { open: "持仓中", closed: "已平仓" },
  position: { candidate: "候选", held: "已持有" },
  // The figures an analyst may cite, by the snapshot's own names for them.
  metric: {
    close: "收盘价", change_1d_pct: "1 日涨跌%", return_5d_pct: "5 日涨跌%", return_1mo_pct: "1 月涨跌%",
    return_3mo_pct: "3 月涨跌%", return_1y_pct: "1 年涨跌%", high_52w: "52 周高点", low_52w: "52 周低点",
    sma20: "20 日均线", sma50: "50 日均线", sma200: "200 日均线", rsi14: "RSI 14", macd: "MACD",
    macd_signal: "MACD 信号线", atr14_pct: "ATR%", trailing_pe: "市盈率", forward_pe: "预期市盈率",
    profit_margin_pct: "利润率%", beta: "Beta", market_cap_billion: "市值（十亿美元）",
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
  if (value === 0) return el("span", { text });
  return el("span", { class: value > 0 ? "up" : "down" }, value > 0 ? "▲ " : "▼ ", text);
}

/** A state with a mark and a label, never colour alone. */
export function state(level, label) {
  return el("span", { class: `state state-${level}`, text: label });
}

export function runState(run) {
  if (!run) return state("idle", "没有运行记录");
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
export function table(columns, rows, none = "没有记录。") {
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
