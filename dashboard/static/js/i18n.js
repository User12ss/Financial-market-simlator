// Which language the page speaks. The page was written in Chinese, and in
// Chinese nothing here does anything: t() hands back what it was given. In
// English, a string of the page's own is looked up by its Chinese in en.js,
// and so is a name the server sends (a seat, a stage, a group). What a model
// or a rule said is never looked up: recorded words are shown as written, in
// whichever language the page is.
//
// The choice is kept in the browser and read once, when the page loads:
// changing it loads the page again in the other language.

import { EN } from "./en.js";

const STORE = "dashboard.lang";
const HAN = /[㐀-鿿]/;

function stored() {
  try {
    return typeof localStorage !== "undefined" ? localStorage.getItem(STORE) : null;
  } catch (error) {
    return null;                                       // a browser that keeps nothing: Chinese, as written
  }
}

export const LANG = stored() === "en" ? "en" : "zh";
export const english = LANG === "en";
const has = (key) => Object.prototype.hasOwnProperty.call(EN, key);

/**
 * A string of the page's own, in the page's language. Called with a plain string, or as a template's tag:
 * t("会议室"), t`共 ${count} 条记录`. A template's key is its fixed parts joined by "{}", and {0}, {1} in the
 * English stand for its values in order. With no English on record the Chinese stands, never a blank.
 */
export function t(strings, ...values) {
  if (typeof strings === "string") return english && has(strings) ? EN[strings] : strings;
  if (english) {
    const key = strings.join("{}");
    if (has(key)) return EN[key].replace(/\{(\d+)\}/g, (whole, place) => (Number(place) < values.length ? String(values[Number(place)]) : whole));
  }
  let text = strings[0];
  for (let at = 0; at < values.length; at += 1) text += String(values[at]) + strings[at + 1];
  return text;
}

// Sentences the server puts together in Chinese (a seat's tally for the day, its record so far): each as a
// pattern with its English. The figures in them are the server's own counts.
const WORD = (text) => (has(text) ? EN[text] : text);
const SENTENCES = [
  [/^(\d+) 条有方向的观点已评分：方向对了 (\d+) 条，跑赢基准 (\d+) 条；另有 (\d+) 条还没到期$/,
    (m) => `${m[1]} directional views scored: direction right in ${m[2]}, beat the benchmark in ${m[3]}; ${m[4]} more not due yet`],
  [/^写过 (\d+) 条观点，还没有一条评分（(\d+) 条没到期）$/, (m) => `Wrote ${m[1]} views, none scored yet (${m[2]} not due)`],
  [/^提出的交易里已平仓 (\d+) 笔（赢 (\d+)，亏 (\d+)，其中止损出场 (\d+)），还开着 (\d+) 笔$/,
    (m) => `Of the trades it proposed, ${m[1]} closed (won ${m[2]}, lost ${m[3]}, ${m[4]} stopped out), ${m[5]} still open`],
  [/^(.+) 买入持有$/, (m) => `${m[1]} buy-and-hold`],
  [/^立场：(.+)，把握度 ([\d.]+)$/, (m) => `Stance: ${WORD(m[1])}, confidence ${m[2]}`],
  [/^沿用 (.+) 的宏观结论$/, (m) => `Macro view carried over from ${m[1]}`],
  [/^(\d+) 条观点：看多 (\d+)，中性 (\d+)，看空 (\d+)$/, (m) => `${m[1]} views: bullish ${m[2]}, neutral ${m[3]}, bearish ${m[4]}`],
  [/^在 (\d+) 场辩论中发言 (\d+) 次$/, (m) => `Spoke ${m[2]} time(s) in ${m[1]} debate(s)`],
  [/^(\d+) 条裁决：持有 (\d+)，不持有 (\d+)$/, (m) => `${m[1]} rulings: hold ${m[2]}, do not hold ${m[3]}`],
  [/^(\d+) 条提案：买入 (\d+)，卖出 (\d+)，减仓 (\d+)，持有 (\d+)$/, (m) => `${m[1]} proposals: buy ${m[2]}, sell ${m[3]}, trim ${m[4]}, hold ${m[5]}`],
  [/^提案：接受 (\d+)，拒绝 (\d+)；订单：批准 (\d+)，削减 (\d+)，拒绝 (\d+)$/,
    (m) => `Proposals: accepted ${m[1]}, rejected ${m[2]}; orders: approved ${m[3]}, reduced ${m[4]}, rejected ${m[5]}`],
  [/^成交 (\d+) 笔，下单 (\d+) 张$/, (m) => `${m[1]} fill(s), ${m[2]} order(s) placed`],
  [/^(\d+) 条更新：不变 (\d+)，加强 (\d+)，削弱 (\d+)，不成立 (\d+)$/,
    (m) => `${m[1]} updates: unchanged ${m[2]}, strengthened ${m[3]}, weakened ${m[4]}, broken ${m[5]}`],
  [/^(\d+) 条决定：保留 (\d+)，减仓 (\d+)，卖出 (\d+)，撤回买单 (\d+)$/,
    (m) => `${m[1]} decisions: keep ${m[2]}, trim ${m[3]}, sell ${m[4]}, withdraw buy ${m[5]}`],
  [/^留给收盘会 (\d+) 条，撤回买单 (\d+) 张，下单 (\d+) 张$/, (m) => `${m[1]} noted for the close, ${m[2]} buy(s) withdrawn, ${m[3]} order(s) placed`],
  [/^账本读不了：([\s\S]*)$/, (m) => `The journal cannot be read: ${m[1]}`],
];

/** One string the server sent, in English when it is one of the server's own Chinese names or sentences. */
export function fromServer(text) {
  if (!HAN.test(text)) return text;                    // recorded words, numbers, ids: as they are
  if (has(text)) return EN[text];
  for (const [pattern, write] of SENTENCES) {
    const found = pattern.exec(text);
    if (found) return write(found);
  }
  return text;
}

/**
 * An answer of the server's as the page should show it. In Chinese, the answer itself. In English, the same
 * answer with the server's own Chinese (names of seats, stages and groups, its tallies) put into English, and
 * every key, number and recorded word exactly as it came.
 */
export function localize(data) {
  if (!english) return data;
  if (typeof data === "string") return fromServer(data);
  if (Array.isArray(data)) return data.map(localize);
  if (data && typeof data === "object") {
    const out = {};
    for (const [key, value] of Object.entries(data)) out[key] = localize(value);
    return out;
  }
  return data;
}

/** Change the page's language: remembered in this browser, and the page loads again in it. */
export function setLang(lang) {
  try {
    localStorage.setItem(STORE, lang === "en" ? "en" : "zh");
  } catch (error) {
    return false;                                      // nothing can be kept here, so nothing would change
  }
  window.location.reload();
  return true;
}
