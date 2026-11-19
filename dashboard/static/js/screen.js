// The market screen in the middle of the room. It follows the discussion:
// while a ticker is being talked about it shows that ticker's recorded daily
// bars with how much it swings; while a macro analyst speaks it shows the
// recorded series of that analyst's field; otherwise the market as a whole.
// Everything on it is read from the data snapshot the run recorded, which is
// what the agents were shown. Prices the record puts on a ticker as the
// meeting goes on (a stop, a limit) are drawn in when their line is reached,
// and what the price did afterwards is joined on only once the record is over.

import { el, svg, clear, showTip, hideTip, usd, num } from "./dom.js";
import { ticks } from "./charts.js";
import { STEPS, lean, reached, stepText } from "./decisions.js";
import {
  BARS_ON_SCREEN, focusOf, gauges, headline, lastBars, levelText, levelsOf, plot, readings, readouts, sectors,
  sinceText, sinceThen, sparkOf, sparkPath, yieldCurve,
} from "./market.js";
import { replay } from "./motion.js";

const STEP = { view: "观点", debate: "辩论", ruling: "裁决", proposal: "提案", check: "检查", risk: "风控", order: "下单" };
const ARROW = { up: "▲ ", down: "▼ ", stop: "✕ ", flat: "" };
const SIZE = { width: 440, height: 150 };
const GUTTER = 50;                    // room at the right of a chart for its prices
const AFTER_ON_SCREEN = 30;
const STEPS_ON_SCREEN = 4;
const WHY = {
  no_run: "还没有运行记录，所以没有行情可看。", no_snapshot: "这一场没有留下行情快照，或者快照文件已经不在了。",
  unreachable: "这一场的行情读不到：看板服务没有给出回答。",
};

const move = (item) => el("i", { class: item.lean === "flat" ? null : item.lean, text: `${ARROW[item.lean]}${item.text}` });

/** One recorded series as a small tile: its name, its value and its recent moves. */
function tile(item) {
  return el("div", { class: "read-tile", title: `截至 ${item.as_of}` },
    el("span", { class: "read-name", text: item.name }),
    el("b", { class: "read-value", text: item.value }),
    el("span", { class: "read-moves" }, item.moves.map((part) => el("span", {}, el("small", { text: part.label }), move(part)))));
}

/** The sector funds as a strip, coloured by their month: how strongly, and which way. */
function sectorStrip(market) {
  const all = sectors(market);
  const most = Math.max(1e-9, ...all.map((point) => Math.abs(point.return_1mo_pct || 0)));
  return el("div", { class: "heat", role: "list", "aria-label": "各板块近一个月的涨跌" }, all.map((point) => {
    const [sector] = readings([point.symbol], market);
    const month = sector.moves[2];
    const strength = Math.round(10 + 60 * (Math.abs(point.return_1mo_pct || 0) / most));
    return el("span", { class: `heat-cell ${month.lean}`, role: "listitem", style: `--heat: ${strength}%` },
      el("small", { text: sector.name }), el("b", { text: `${ARROW[month.lean]}${month.text}` }));
  }));
}

/** The recorded Treasury yields as a curve from the shortest to the longest. */
function curveChart(market) {
  const curve = yieldCurve(market);
  if (curve.length < 3) return null;
  const width = 200, height = 64, pad = 16;
  const low = Math.min(...curve.map((point) => point.value)), high = Math.max(...curve.map((point) => point.value));
  const x = (at) => pad + (at / (curve.length - 1)) * (width - 2 * pad);
  const y = (value) => 10 + ((high - value) / (high - low || 1)) * (height - 34);
  const line = curve.map((point, at) => `${at ? "L" : "M"}${x(at).toFixed(1)},${y(point.value).toFixed(1)}`).join(" ");
  return svg("svg", {
    class: "curve", viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `国债收益率曲线：${curve.map((point) => `${point.label} ${point.text}`).join("，")}`,
  },
    svg("path", { class: "curve-line", d: line }),
    curve.map((point, at) => [
      svg("circle", { class: "curve-dot", cx: x(at), cy: y(point.value), r: 3 }),
      svg("text", { class: "curve-value", x: x(at), y: y(point.value) - 6, "text-anchor": "middle", text: point.text }),
      svg("text", { class: "curve-label", x: x(at), y: height - 4, "text-anchor": "middle", text: point.label }),
    ]));
}

export function marketScreen({ personas }) {
  const state = { market: null, shown: null, size: { ...SIZE }, context: null };
  const head = el("div", { class: "holo-head" });
  const body = el("div", { class: "holo-body" });
  const foot = el("div", { class: "holo-foot" });
  const node = el("section", { class: "holo", "aria-label": "行情屏" }, head, body, foot);

  /** A ticker's bars as candles. `levels` are the prices the record has put on it so far; `after` joins on what came later. */
  function candleChart(ticker, before, later, levels, marks, { width, height }) {
    const inner = Math.max(120, width - GUTTER);
    const drawn = plot(before, later, levels, { width: inner, height });
    const inside = (price) => price <= drawn.high && price >= drawn.low;
    const today = drawn.candles[before.length - 1];
    const frame = svg("svg", {
      class: "candles", viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": `${ticker} 最近 ${before.length} 个交易日的日 K 线，这一场的收盘价 ${usd(today.close)}`
        + (later.length ? `，后面接着之后 ${later.length} 个交易日` : ""),
    });
    for (const price of ticks(drawn.low, drawn.high, 4).filter(inside)) {
      frame.append(svg("line", { class: "grid-line", x1: 0, x2: inner, y1: drawn.y(price), y2: drawn.y(price) }),
        svg("text", { class: "price", x: inner + 6, y: drawn.y(price) + 3.5, text: num(price, 2) }));
    }
    if (drawn.cut !== null) {
      frame.append(svg("rect", { class: "after-zone", x: drawn.cut, y: 0, width: inner - drawn.cut, height }),
        svg("line", { class: "cut", x1: drawn.cut, x2: drawn.cut, y1: 0, y2: height }),
        svg("text", { class: "cut-label", x: drawn.cut + 4, y: 10, text: "之后的实际走势（当时看不到）" }));
    }
    // How far the price usually moves in a day (the recorded ATR), as a band round the session's close.
    if (marks.swing) {
      const top = Math.max(0, drawn.y(Math.min(drawn.high, marks.swing.high)));
      const base = Math.min(drawn.bottom, drawn.y(Math.max(drawn.low, marks.swing.low)));
      frame.append(svg("rect", {
        class: "swing", x: today.x, y: top, width: Math.max(0, (drawn.cut ?? inner) - today.x), height: Math.max(0, base - top),
      }));
    }
    for (const line of marks.lines.filter((average) => inside(average.price))) {
      frame.append(svg("line", { class: `avg ${line.key}`, x1: Math.max(0, today.x - 46), x2: today.x, y1: drawn.y(line.price), y2: drawn.y(line.price) }),
        svg("text", { class: "avg-label", x: Math.max(0, today.x - 46), y: drawn.y(line.price) - 3, text: line.key.replace("sma", "MA") }));
    }
    drawn.candles.forEach((bar, at) => {
      const kind = `${bar.up ? "up" : "down"}${bar.after ? " after" : ""}`;
      const top = Math.min(bar.yOpen, bar.yClose);
      frame.append(svg("g", { class: `candle ${kind}`, style: `--i: ${at}` },
        svg("rect", { class: "vol", x: bar.x - bar.w / 2, y: bar.yVolume, width: bar.w, height: Math.max(0, height - bar.yVolume) }),
        svg("line", { class: "wick", x1: bar.x, x2: bar.x, y1: bar.yHigh, y2: bar.yLow }),
        svg("rect", { class: "body", x: bar.x - bar.w / 2, y: top, width: bar.w, height: Math.max(1, Math.abs(bar.yOpen - bar.yClose)) })));
    });
    for (const level of drawn.levels.filter((placed) => !placed.off)) {
      frame.append(svg("line", { class: `level ${level.kind}`, x1: 0, x2: inner, y1: level.y, y2: level.y }),
        svg("text", { class: `level-label ${level.kind}`, x: 4, y: level.y - 4, text: levelText(level) }));
    }
    frame.append(svg("circle", { class: "last-ring", cx: today.x, cy: today.yClose, r: 6 }),
      svg("circle", { class: "last-dot", cx: today.x, cy: today.yClose, r: 2.6 }),
      svg("rect", { class: "last-tag", x: inner + 2, y: today.yClose - 8, width: GUTTER - 4, height: 16, rx: 3 }),
      svg("text", { class: "last-price", x: inner + 6, y: today.yClose + 3.8, text: num(today.close, 2) }));
    // One strip per session to point at: the day and its open, high, low, close and volume.
    drawn.candles.forEach((bar) => {
      const strip = svg("rect", { class: "hit", x: bar.x - drawn.step / 2, y: 0, width: drawn.step, height });
      strip.addEventListener("pointermove", (pointer) => showTip(pointer.clientX, pointer.clientY,
        `${ticker} · ${bar.date}${bar.after ? " · 这一场之后" : ""}`, [
          { value: usd(bar.open), name: "开盘" }, { value: usd(bar.high), name: "最高" }, { value: usd(bar.low), name: "最低" },
          { value: usd(bar.close), name: "收盘" }, { value: num(bar.volume), name: "成交量（股）" },
        ]));
      strip.addEventListener("pointerleave", hideTip);
      frame.append(strip);
    });
    return frame;
  }

  function showTicker(ticker, context) {
    const item = state.market.tickers[ticker];
    if (!item || !item.price || item.bars.dates.length === 0) {
      head.append(el("span", { class: "holo-tag", text: ticker }), el("span", { class: "holo-name", text: "这只股票不在这一场的行情快照里" }));
      body.append(el("p", { class: "holo-empty", text: "这一场的候选和持仓里没有它（多半是之前的订单在今天成交或撤回），所以没有它的 K 线可看。" }));
      return;
    }
    const day = readouts(item)[1];
    head.append(
      el("span", { class: "holo-tag", text: ticker }),
      el("span", { class: "holo-name", text: [item.name, item.sector].filter(Boolean).join(" · ") }),
      el("span", { class: "holo-price" }, el("b", { text: usd(item.price.close) }), move(day)));
    const row = context.rows.get(ticker) || null;
    const levels = levelsOf(row, context.events, context.index);
    const since = context.ended ? sinceThen(item) : null;
    // How far this ticker's decision has got by the line being played, beside its price: the latest few steps
    // (the trail under the room has all of them).
    const steps = row ? STEPS.filter((name) => reached(row[name], context.index)).slice(-STEPS_ON_SCREEN) : [];
    if (steps.length) {
      body.append(el("div", { class: "holo-steps" }, steps.map((name) => {
        const way = lean(name, row[name]);
        return el("span", { class: "holo-step", "data-lean": way },
          el("small", { text: STEP[name] }), `${ARROW[way]}${stepText(name, row[name], { brief: true, upTo: context.index })}`);
      })));
    }
    const later = since && item.after ? lastBars(item.after, Infinity).slice(0, AFTER_ON_SCREEN) : [];
    const before = lastBars(item.bars, BARS_ON_SCREEN - later.length);
    // A level too far from the candles to be drawn among them is named under the chart instead.
    const far = plot(before, later, levels, SIZE).levels.filter((level) => level.off);
    const plotBox = el("div", { class: "holo-plot" });
    body.append(plotBox);
    foot.append(
      el("div", { class: "reads" }, readouts(item).slice(2).map((part) =>
        el("span", { class: "read" }, el("small", { text: part.label }), el("b", { class: part.lean === "flat" ? null : part.lean, text: part.text })))),
      el("div", { class: "holo-notes" },
        far.map((level) => el("span", { class: `note-level ${level.kind}`, text: levelText(level) })),
        since ? el("span", { class: `note-since ${since.lean}`, text: sinceText(since) }) : null,
        context.ended && !since ? el("span", { class: "muted", text: "这一场之后还没有新的交易日记录。" }) : null));
    // Drawn last, at the size the screen leaves for it once everything round it is in place.
    const wide = plotBox.clientWidth, tall = plotBox.clientHeight;
    plotBox.append(candleChart(ticker, before, later, levels, gauges(item), wide > 40 && tall > 40 ? { width: wide, height: tall } : SIZE));
  }

  /** The tickers on the portfolio manager's list, side by side, each with what was proposed for it. */
  function showList(tickers, context) {
    head.append(el("span", { class: "holo-tag", text: "提案" }), el("span", { class: "holo-name", text: "组合经理这份清单上的股票" }));
    if (tickers.length === 0) {
      body.append(el("p", { class: "holo-empty", text: "这份清单上没有任何股票。" }));
      return;
    }
    body.append(el("div", { class: "holo-list" }, tickers.map((ticker) => {
      const item = state.market.tickers[ticker], row = context.rows.get(ticker);
      const proposed = row && reached(row.proposal, context.index) ? row.proposal : null;
      const line = item && item.price ? sparkPath(sparkOf(item), 96, 26) : null;
      const day = item && item.price ? readouts(item)[1] : null;
      return el("div", { class: "mini" },
        el("div", { class: "mini-top" }, el("b", { text: ticker }),
          proposed ? el("span", { class: "holo-step", "data-lean": lean("proposal", proposed), text: `${ARROW[lean("proposal", proposed)]}${stepText("proposal", proposed)}` }) : null),
        line ? svg("svg", { class: `spark ${day.lean}`, viewBox: "0 0 96 26", "aria-hidden": "true" }, svg("path", { d: line })) : null,
        item && item.price ? el("div", { class: "mini-price" }, usd(item.price.close), " ", move(day)) : el("div", { class: "muted", text: "快照里没有它" }));
    })));
  }

  /** The recorded series of one macro analyst's field. */
  function showDesk(persona) {
    const found = readings(persona.reads.filter((id) => id !== "sectors"), state.market);
    const wantsSectors = persona.reads.includes("sectors");
    const wantsCurve = persona.reads.filter((id) => ["^IRX", "^FVX", "^TNX", "^TYX"].includes(id)).length >= 2;
    head.append(el("span", { class: "holo-tag", text: persona.title }), el("span", { class: "holo-name", text: persona.craft.join(" · ") }));
    // A chart is nothing at all when the snapshot holds none of its series: a missing one is left out, not
    // handed to the page, which would write the word "null" in its place.
    body.append(...[
      el("div", { class: "read-grid" }, found.map(tile)),
      wantsCurve ? curveChart(state.market) : null, wantsSectors ? sectorStrip(state.market) : null,
    ].filter(Boolean));
    foot.append(el("div", { class: "holo-notes muted", text: "记录里和这个领域有关的数据。每位宏观分析师拿到的是同一张完整的数据表。" }));
  }

  function showOverview() {
    head.append(el("span", { class: "holo-tag", text: "大盘" }), el("span", { class: "holo-name", text: "这一场用的行情快照" }));
    body.append(...[
      el("div", { class: "read-grid" }, headline(state.market).map(tile)), curveChart(state.market), sectorStrip(state.market),
    ].filter(Boolean));
  }

  function paint() {
    const context = state.context;
    clear(head);
    clear(body);
    clear(foot);
    hideTip();
    if (!state.market || !state.market.available) {
      head.append(el("span", { class: "holo-tag", text: "行情" }));
      body.append(el("p", { class: "holo-empty", text: state.market ? WHY[state.market.reason] || WHY.no_snapshot : "正在读这一场的行情快照。" }));
      return;
    }
    // Put in first and shown last (the style sheet orders it), so a chart is measured with everything round it in place.
    foot.append(el("div", { class: "holo-asof", text: `截至 ${state.market.session} 收盘 · 日线` }));
    const focus = focusOf(context.event, personas);
    if (focus.kind === "ticker") showTicker(focus.ticker, context);
    else if (focus.kind === "list") showList(focus.tickers, context);
    else if (focus.kind === "desk") showDesk(personas.get(focus.persona));
    else showOverview();
  }

  /** What the screen would show for this context, as a short text: it is drawn again only when this changes. */
  function signature(context) {
    if (!state.market || !state.market.available) return `none:${state.market ? state.market.reason : ""}`;
    const focus = focusOf(context.event, personas);
    const about = focus.kind === "ticker" ? [focus.ticker] : focus.kind === "list" ? focus.tickers : [];
    const progress = about.map((ticker) => {
      const row = context.rows.get(ticker);
      return row ? STEPS.map((name) => (reached(row[name], context.index) ? stepText(name, row[name], { upTo: context.index }) : "")).join("|") : "";
    });
    return { about: JSON.stringify([state.market.run_id, focus]), all: JSON.stringify([state.market.run_id, focus, progress, context.ended]) };
  }

  /** Take the market data of the run in the room. */
  function load(market) {
    state.market = market;
    state.shown = null;
    if (state.context) show(state.context);
  }

  /**
   * Show what belongs to one line of the record. `rows` are the decision
   * trail's rows by ticker, `index` the line being played, and `ended` says
   * the record is over at this line.
   */
  function show(context) {
    state.context = context;
    const now = signature(context);
    if (state.shown && now.all === state.shown.all) return;
    const turned = !state.shown || now.about !== state.shown.about;       // the screen is about something else now
    state.shown = now;
    paint();
    // A sweep, and candles that rise into place, only when the subject changes: one more step about the same
    // ticker is added to a chart that stays as it is.
    if (turned) replay(node, "scan");
    else node.classList.remove("scan");
  }

  // When the screen changes size, a chart is drawn again to fit it.
  if (typeof ResizeObserver !== "undefined") {
    let size = null;
    new ResizeObserver(() => {
      const now = `${Math.round(node.clientWidth)}x${Math.round(node.clientHeight)}`;
      if (now === size) return;
      const first = size === null;
      size = now;
      if (!first && state.context) paint();
    }).observe(node);
  }

  return { node, load, show };
}
