// What stands behind the meeting: the market the team is deciding in. On the
// wall of the room, where every ticker of this meeting's snapshot has been
// over the last sixty sessions, each drawn from the same starting point, so
// that how far they have moved apart can be seen at a glance, with the one
// being talked about picked out. Along the top of the room, the hour: the
// moment of the line being played, the cut-off of what the team knew, the
// session the prices are from, and how unsettled the market is by the record's
// own measures. All of it is read off the run's snapshot and its lines.

import { el, svg, clear, fill, signedPct, num } from "./dom.js";
import { t } from "./i18n.js";

export const WALL_SESSIONS = 60;
const NEW_YORK = { timeZone: "America/New_York", hour12: false };
const given = (value) => typeof value === "number" && Number.isFinite(value);
const STATES = { calm: t("平静"), turbulent: t("动荡") };

/**
 * Each ticker's recorded closes over the last `count` sessions, as percent
 * from the first of them, in the order of their names. A ticker with fewer
 * than two closes on record has no path.
 */
export function pathsOf(market, count = WALL_SESSIONS) {
  if (!market || !market.available) return [];
  return Object.entries(market.tickers)
    .map(([ticker, item]) => [ticker, item.bars ? item.bars.close.slice(-count).filter(given) : []])
    .filter(([, closes]) => closes.length >= 2 && closes[0] > 0)
    .map(([ticker, closes]) => {
      const moves = closes.map((close) => (close / closes[0] - 1) * 100);
      return { ticker, moves, change: moves[moves.length - 1] };
    })
    .sort((a, b) => a.ticker.localeCompare(b.ticker));
}

/** How far apart the paths have ended: the strongest, the weakest, and how many rose and fell. Null with none. */
export function spreadOf(paths) {
  if (paths.length === 0) return null;
  const ranked = [...paths].sort((a, b) => b.change - a.change);
  return {
    best: ranked[0], worst: ranked[ranked.length - 1], count: paths.length,
    up: paths.filter((path) => path.change > 0).length, down: paths.filter((path) => path.change < 0).length,
  };
}

/** Where the paths sit in a box: across by session, up by percent, the same scale for all of them. */
export function wallPlot(paths, { x, y, width, height }) {
  const all = paths.flatMap((path) => path.moves);
  const low = Math.min(0, ...all), high = Math.max(0, ...all), spread = high - low || 1;
  const longest = Math.max(2, ...paths.map((path) => path.moves.length));
  const across = (at, length) => x + ((longest - length + at) / (longest - 1)) * width;       // a short path ends where the rest do
  const up = (move) => y + ((high - move) / spread) * height;
  return {
    low, high, zero: up(0), up,
    lines: paths.map((path) => ({
      ticker: path.ticker, change: path.change, lean: path.change > 0 ? "up" : path.change < 0 ? "down" : "flat",
      d: path.moves.map((move, at) => `${at ? "L" : "M"}${across(at, path.moves.length).toFixed(1)},${up(move).toFixed(1)}`).join(" "),
      end: [x + width, up(path.change)],
    })),
  };
}

/**
 * The wall behind the room, as a group for the room's own drawing. `load(market)` takes the run's market data
 * and `focus(ticker)` picks out the path of the ticker being talked about, or none.
 */
export function marketWall(box) {
  const node = svg("g", { class: "wall", "aria-hidden": "true" });
  const state = { lines: new Map(), tag: null, focused: null };

  function load(market) {
    clear(node);
    state.lines.clear();
    state.tag = null;
    const paths = pathsOf(market);
    if (paths.length === 0) return;
    const drawn = wallPlot(paths, box);
    // The scale, on the wall's right edge: the level they all started from, and how far the furthest two went.
    for (const [move, text] of [[drawn.high, signedPct(drawn.high, 0)], [0, "0%"], [drawn.low, signedPct(drawn.low, 0)]]) {
      if (move !== 0 && Math.abs(drawn.up(move) - drawn.zero) < 14) continue;      // too near the starting level to label apart
      node.append(svg("line", { class: move === 0 ? "wall-zero" : "wall-grid", x1: box.x, x2: box.x + box.width, y1: drawn.up(move), y2: drawn.up(move) }),
        svg("text", { class: "wall-tick", x: box.x + box.width + 5, y: drawn.up(move) + 3, text }));
    }
    for (const line of drawn.lines) {
      const path = svg("path", { class: `wall-line ${line.lean}`, d: line.d, pathLength: 1 });
      state.lines.set(line.ticker, { ...line, path });
      node.append(path);
    }
    // Where the path of the ticker in hand ends: a point that beats. Its name and figure are in the strip along the
    // top of the room, where they are in nobody's way.
    state.tag = svg("g", { class: "wall-tag", visibility: "hidden" },
      svg("circle", { class: "wall-ping", r: 5 }), svg("circle", { class: "wall-dot", r: 2.6 }));
    node.append(state.tag);
    focus(state.focused);
  }

  function focus(ticker) {
    state.focused = ticker || null;
    for (const [name, line] of state.lines) line.path.classList.toggle("on", name === state.focused);
    if (!state.tag) return;
    const line = state.focused ? state.lines.get(state.focused) : null;
    state.tag.setAttribute("visibility", line ? "visible" : "hidden");
    if (!line) return;
    state.tag.setAttribute("transform", `translate(${line.end[0].toFixed(1)},${line.end[1].toFixed(1)})`);
    state.tag.setAttribute("class", `wall-tag ${line.lean}`);
  }

  return { node, load, focus };
}

/** A moment as the New York clock showed it: the market's own time. */
export function nyClock(iso, seconds = true) {
  if (!iso) return null;
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return null;
  return moment.toLocaleTimeString("en-GB", { ...NEW_YORK, hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) });
}

/**
 * The market's state as the quant engineer recorded it, by the line being played: the chance of each state after
 * the session's close. Null until that line has been reached, or when the record has none.
 */
export function marketState(events, index) {
  for (let at = Math.min(index, events.length - 1); at >= 0; at -= 1) {
    const event = events[at];
    if (event.kind !== "market_state") continue;
    const said = event.payload || {};
    if (!Array.isArray(said.states) || !Array.isArray(said.now)) return null;
    return { index: at, states: said.states.map((name, place) => ({ name: STATES[name] || name, share: said.now[place] })) };
  }
  return null;
}

/**
 * The strip along the top of the room: what time it is in the meeting, and what the market is like.
 * `update({ event, events, index, run, market, paths })` redraws it.
 */
export function statusStrip() {
  const node = el("div", { class: "status", role: "group", "aria-label": t("会议的时刻和行情的状态") });
  const cell = (label, value, more = {}) => el("span", { class: `status-cell${more.kind ? ` ${more.kind}` : ""}`, title: more.title || null },
    el("small", { text: label }), typeof value === "string" ? el("b", { class: more.lean || null, text: value }) : value);

  function update({ event, events, index, run, market, focused }) {
    const cells = [];
    const moment = event ? nyClock(event.ts) : null;
    cells.push(cell(t("会议时刻（纽约）"), moment || "—", { kind: "clock", title: t("正在播放的这一句是什么时候记下的") }));
    if (run && run.as_of) cells.push(cell(t("信息截止"), nyClock(run.as_of, false), { title: t("这一刻之后才取得的消息，进不了这场会") }));
    if (market && market.available) {
      cells.push(cell(t("行情截至"), t`${market.session} 收盘`, { title: t("会上看到的价格是这个交易日的日线") }));
      const vix = market.market.find((point) => point.symbol === "^VIX");
      if (vix && given(vix.value)) {
        const lean = !given(vix.change_1d_pct) || vix.change_1d_pct === 0 ? null : vix.change_1d_pct > 0 ? "up" : "down";
        cells.push(cell(t("VIX 波动率"), el("b", {}, num(vix.value, 2), given(vix.change_1d_pct)
          ? el("i", { class: lean, text: ` ${lean === "up" ? "▲" : lean === "down" ? "▼" : ""} ${signedPct(vix.change_1d_pct)}` }) : null),
          { title: t("市场预期的波动。越高，越不平静") }));
      }
      const paths = pathsOf(market);
      const spread = spreadOf(paths);
      if (spread) {
        cells.push(cell(t`${spread.count} 只股票近 ${WALL_SESSIONS} 日`, el("b", {},
          el("i", { class: "up", text: `▲ ${spread.up}` }), " ", el("i", { class: "down", text: `▼ ${spread.down}` }),
          el("span", { class: "status-range", text: ` ${spread.worst.ticker} ${signedPct(spread.worst.change, 1)} … ${spread.best.ticker} ${signedPct(spread.best.change, 1)}` })),
          { title: t("背景里的细线：每只股票最近 60 个交易日的收盘价走势，起点对齐。分得越开，差别越大") }));
      }
      // The ticker in hand: where its own path on the wall has got to, and how much it usually moves in a day.
      const item = focused ? market.tickers[focused] : null;
      const own = focused ? paths.find((path) => path.ticker === focused) : null;
      const swing = item && item.technicals && given(item.technicals.atr14_pct) ? item.technicals.atr14_pct : null;
      if (own || swing !== null) {
        const lean = !own || own.change === 0 ? null : own.change > 0 ? "up" : "down";
        cells.push(cell(t`正在讨论 ${focused}`, el("b", {},
          own ? el("i", { class: lean, text: t`近 ${WALL_SESSIONS} 日 ${lean === "up" ? "▲ " : lean === "down" ? "▼ " : ""}${signedPct(own.change, 1)}` }) : null,
          own && swing !== null ? " · " : null, swing !== null ? t`日常波幅 ${num(swing, 2)}%` : null),
          { kind: "hand", title: t("背景里加粗的那条线是它。日常波幅是它平常一天上下动多少（记录的 ATR）") }));
      }
    }
    const mood = marketState(events, index);
    if (mood) {
      cells.push(cell(t("市场状态（量化工程师）"), el("b", {}, mood.states.map((part, at) =>
        el("span", { class: "status-share" }, at ? " · " : "", `${part.name} ${Math.round(part.share * 100)}%`))),
        { kind: "mood", title: t("量化工程师（代码）记录的：收盘后市场处在每种状态的可能性。是估计，不是测量") }));
    }
    fill(node, ...cells);
  }

  return { node, update };
}
