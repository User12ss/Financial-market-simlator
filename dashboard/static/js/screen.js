// The market screen in the middle of the room. It follows the discussion:
// while a ticker is being talked about it shows that ticker's recorded daily
// bars with how much it swings; while a macro analyst speaks it shows the
// recorded series of that analyst's field; otherwise the market as a whole.
// Everything on it is read from the data snapshot the run recorded, which is
// what the agents were shown. Prices the record puts on a ticker as the
// meeting goes on (a stop, a limit) are drawn in when their line is reached,
// and what the price did afterwards is joined on only once the record is over.

import { el, svg, clear, fill, showTip, hideTip, usd, num, signedPct } from "./dom.js";
import { ticks } from "./charts.js";
import { STEPS, lean, reached, stepText } from "./decisions.js";
import {
  BARS_ON_SCREEN, fanOf, fanText, focusOf, gauges, headline, lastBars, levelText, levelsOf, plot, readings, readouts,
  sectors, sinceText, sinceThen, sparkOf, sparkPath, trendPlot, yieldCurve,
} from "./market.js";
import { replay } from "./motion.js";
import { english, t } from "./i18n.js";

const STEP = { view: t("观点"), debate: t("辩论"), ruling: t("裁决"), proposal: t("提案"), check: t("检查"), risk: t("风控"), order: t("下单") };
const ARROW = { up: "▲ ", down: "▼ ", stop: "✕ ", flat: "" };
const SIZE = { width: 440, height: 150 };
const GUTTER = 50;                    // room at the right of a chart for its prices
const AFTER_ON_SCREEN = 30;
const STEPS_ON_SCREEN = 4;
const WHY = {
  no_run: t("还没有运行记录，所以没有行情可看。"), no_snapshot: t("这一场没有留下行情快照，或者快照文件已经不在了。"),
  unreachable: t("这一场的行情读不到：看板服务没有给出回答。"),
};

const move = (item) => el("i", { class: item.lean === "flat" ? null : item.lean, text: `${ARROW[item.lean]}${item.text}` });

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A count of shares in the units a reader counts them in: 万 and 亿 in Chinese, K, M and B in English. */
export function shares(count) {
  if (typeof count !== "number" || !Number.isFinite(count)) return "—";
  if (english) return count >= 1e9 ? `${(count / 1e9).toFixed(2)}B` : count >= 1e6 ? `${(count / 1e6).toFixed(2)}M` : count >= 1e3 ? `${(count / 1e3).toFixed(1)}K` : String(Math.round(count));
  if (count >= 1e8) return t`${(count / 1e8).toFixed(2)} 亿`;
  if (count >= 1e4) return t`${(count / 1e4).toFixed(1)} 万`;
  return String(Math.round(count));
}

/**
 * One session of a chart in a line, as a terminal's legend gives it: the day, its open, high, low and close, the
 * change from the close before, and the volume. `bars` are the chart's sessions in order and `at` the one meant.
 */
export function legendOf(bars, at) {
  const bar = bars[at], before = at > 0 ? bars[at - 1] : null;
  const change = before ? bar.close - before.close : null;
  const lean = change === null || change === 0 ? "flat" : change > 0 ? "up" : "down";
  const tone = lean === "flat" ? null : lean;
  const sign = change > 0 ? "+" : change < 0 ? "−" : "";
  return [
    el("span", { class: "legend-day", text: `${bar.date.slice(5)}${bar.after ? t(" · 之后") : ""}` }),
    ...[[t("开"), bar.open], [t("高"), bar.high], [t("低"), bar.low], [t("收"), bar.close]].map(([label, value]) =>
      el("span", {}, el("small", { text: label }), el("b", { class: tone, text: num(value, 2) }))),
    change === null ? null : el("b", { class: tone, text: `${ARROW[lean]}${sign}${num(Math.abs(change), 2)} (${signedPct((change / before.close) * 100)})` }),
    el("span", {}, el("small", { text: t("量") }), el("b", { text: shares(bar.volume) })),
  ].filter(Boolean);
}

const TREND = { width: 120, height: 30 };
const AGO = { 0: t("这一场"), 1: t("前一个交易日"), 5: t("5 个交易日前"), 21: t("一个月前（21 个交易日）") };

/**
 * A series' recent path as a small line. With daily closes on record it is their line; with only the snapshot's
 * four values (now, one session, five and twenty-one back) it is those four joined by a dashed line with a mark
 * on each, so that nobody takes the straight stretches between them for the path the series took.
 */
function trendLine(item) {
  if (!item.trend) return null;
  const drawn = trendPlot(item.trend, TREND.width, TREND.height);
  const sparse = item.trend.kind === "marks";
  const said = sparse
    ? t`${item.name} 的四个记录值连成的线：${drawn.dots.map((dot) => `${AGO[dot.back] || t`${dot.back} 个交易日前`} ${num(dot.value, 2)}`).join("，")}`
    : t`${item.name} 最近 ${drawn.dots.length} 个交易日的收盘走势`;
  return svg("svg", {
    class: `trend ${drawn.lean}${sparse ? " sparse" : ""}`, viewBox: `0 0 ${TREND.width} ${TREND.height}`,
    preserveAspectRatio: "none", role: "img", "aria-label": said,
  },
    svg("path", { class: "trend-area", d: drawn.area }),
    svg("path", { class: "trend-line", d: drawn.line, pathLength: 1 }),
    // A mark is a line of no length with a round end: the plot is stretched to its tile, and a circle would be too.
    sparse ? drawn.dots.map((dot) => svg("path", { class: "trend-dot", d: `M${dot.x.toFixed(1)},${dot.y.toFixed(1)} l0,0` })) : null,
    svg("path", { class: "trend-last", d: `M${drawn.dots[drawn.dots.length - 1].x.toFixed(1)},${drawn.dots[drawn.dots.length - 1].y.toFixed(1)} l0,0` }));
}

/** One recorded series as a small tile: its name and value, where it has been lately, and its recent moves. */
function tile(item) {
  const line = trendLine(item);
  const node = el("div", { class: "read-tile", title: t`截至 ${item.as_of}` },
    el("div", { class: "read-top" }, el("span", { class: "read-name", text: item.name }), el("b", { class: "read-value", text: item.value })),
    line,
    el("span", { class: "read-moves" }, item.moves.map((part) => el("span", {}, el("small", { text: part.label }), move(part)))));
  // Pointing at a tile says what its line is made of: the values themselves, never hover-only (they are its label too).
  const rows = item.trend
    ? item.trend.points.slice(item.trend.kind === "marks" ? 0 : -1).map((point) =>
      ({ value: num(point.value, 2), name: AGO[point.back] || t`${point.back} 个交易日前` }))
    : [];
  node.addEventListener("pointermove", (pointer) => showTip(pointer.clientX, pointer.clientY, t`${item.name} · 截至 ${item.as_of}`, [
    ...rows,
    { value: item.trend && item.trend.kind === "daily" ? t`线是最近 ${item.trend.points.length} 个交易日的收盘价` : item.trend
      ? t("虚线只连着记录里有的四个值，中间怎么走的没有记录") : t("这项数据没有记录走势"), name: "", plain: true },
  ]));
  node.addEventListener("pointerleave", hideTip);
  return node;
}

/** The sector funds as a strip, coloured by their month: how strongly, and which way. */
function sectorStrip(market) {
  const all = sectors(market);
  const most = Math.max(1e-9, ...all.map((point) => Math.abs(point.return_1mo_pct || 0)));
  return el("div", { class: "heat", role: "list", "aria-label": t("各板块近一个月的涨跌") }, all.map((point) => {
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
    "aria-label": t`国债收益率曲线：${curve.map((point) => `${point.label} ${point.text}`).join("，")}`,
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
  const node = el("section", { class: "holo", "aria-label": t("行情屏") }, head, body, foot);

  /**
   * A ticker's bars as candles. `levels` are the prices the record has put on it so far; `after` joins on what
   * came later; `fan` is the quant engineer's forecast, once its line has been reached.
   */
  function candleChart(ticker, before, later, levels, marks, { width, height }, fan = null, onBar = null) {
    const inner = Math.max(120, width - GUTTER);
    const drawn = plot(before, later, levels, { width: inner, height, fan });
    const inside = (price) => price <= drawn.high && price >= drawn.low;
    const today = drawn.candles[before.length - 1];
    const frame = svg("svg", {
      class: "candles", viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": t`${ticker} 最近 ${before.length} 个交易日的日 K 线，这一场的收盘价 ${usd(today.close)}`
        + (later.length ? t`，后面接着之后 ${later.length} 个交易日` : "")
        + (drawn.fan ? t`。右侧是预测的分布：${fanText(drawn.fan.points[drawn.fan.points.length - 1])}` : ""),
    });
    for (const price of ticks(drawn.low, drawn.high, 4).filter(inside)) {
      frame.append(svg("line", { class: "grid-line", x1: 0, x2: inner, y1: drawn.y(price), y2: drawn.y(price) }),
        svg("text", { class: "price", x: inner + 6, y: drawn.y(price) + 3.5, text: num(price, 2) }));
    }
    // The months along the bottom: a mark where each begins, as on any terminal's time axis.
    drawn.candles.forEach((bar, at) => {
      const month = bar.date.slice(0, 7);
      if (at === 0 || drawn.candles[at - 1].date.slice(0, 7) === month) return;
      frame.append(svg("line", { class: "month-line", x1: bar.x, x2: bar.x, y1: 0, y2: height }),
        svg("text", { class: "month", x: bar.x + 3, y: height - 2, text: english ? MONTHS[Number(bar.date.slice(5, 7)) - 1] : t`${Number(bar.date.slice(5, 7))}月` }));
    });
    if (drawn.cut !== null) {
      frame.append(svg("rect", { class: "after-zone", x: drawn.cut, y: 0, width: inner - drawn.cut, height }),
        svg("line", { class: "cut", x1: drawn.cut, x2: drawn.cut, y1: 0, y2: height }),
        svg("text", { class: "cut-label", x: drawn.cut + 4, y: 10, text: t("之后的实际走势（当时看不到）") }));
    }
    // The forecast, under the candles: what was recorded afterwards is drawn over it, so the two can be compared.
    if (drawn.fan) {
      const { x, y, points } = drawn.fan;
      const edge = (name) => points.map((point) => `L${point.x.toFixed(1)},${point[name].toFixed(1)}`).join(" ");
      const back = (name) => [...points].reverse().map((point) => `L${point.x.toFixed(1)},${point[name].toFixed(1)}`).join(" ");
      const far = points[points.length - 1];
      frame.append(
        svg("path", { class: "fan wide", d: `M${x.toFixed(1)},${y.toFixed(1)} ${edge("y95")} ${back("y5")} Z` }),
        svg("path", { class: "fan half", d: `M${x.toFixed(1)},${y.toFixed(1)} ${edge("y75")} ${back("y25")} Z` }),
        svg("path", { class: "fan-mid", d: `M${x.toFixed(1)},${y.toFixed(1)} ${edge("y50")}` }));
      // Named on the chart while there is room: once the bars recorded afterwards are joined on, their own
      // heading stands there, and the line under the chart says what the fan is.
      if (drawn.cut === null) {
        frame.append(
          svg("text", { class: "fan-label", x: far.x, y: Math.max(9, far.y95 - 3), "text-anchor": "end", text: t("九成的路径") }),
          svg("text", { class: "fan-label", x: far.x, y: Math.max(9, far.y75 - 3), "text-anchor": "end", text: t("一半") }));
      }
    }
    // How far the price usually moves in a day (the recorded ATR), as a band round the session's close.
    if (marks.swing && !drawn.fan) {
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
    // Two levels at nearly the same price (the analyst's stop and the one the order carries, say) would be written
    // one over the other: the later one is written after the earlier, on the same line.
    const written = [];
    for (const level of drawn.levels.filter((placed) => !placed.off)) {
      const text = levelText(level);
      const x = 4 + written.filter((other) => Math.abs(other.y - level.y) < 11).reduce((sum, other) => sum + other.width + 12, 0);
      written.push({ y: level.y, width: Array.from(text).reduce((sum, letter) => sum + (letter.charCodeAt(0) > 255 ? 10 : 6.1), 0) });
      frame.append(svg("line", { class: `level ${level.kind}`, x1: 0, x2: inner, y1: level.y, y2: level.y }),
        svg("text", { class: `level-label ${level.kind}`, x, y: level.y - 4, text }));
    }
    frame.append(svg("circle", { class: "last-ring", cx: today.x, cy: today.yClose, r: 6 }),
      svg("circle", { class: "last-dot", cx: today.x, cy: today.yClose, r: 2.6 }),
      svg("rect", { class: "last-tag", x: inner + 2, y: today.yClose - 8, width: GUTTER - 4, height: 16, rx: 3 }),
      svg("text", { class: "last-price", x: inner + 6, y: today.yClose + 3.8, text: num(today.close, 2) }));
    // One strip per session to point at: the day and its open, high, low, close and volume.
    const hair = svg("line", { class: "hair", y1: 0, y2: height, visibility: "hidden" });
    frame.append(hair);
    drawn.candles.forEach((bar, at) => {
      const strip = svg("rect", { class: "hit", x: bar.x - drawn.step / 2, y: 0, width: drawn.step, height });
      strip.addEventListener("pointermove", (pointer) => {
        showTip(pointer.clientX, pointer.clientY,
          `${ticker} · ${bar.date}${bar.after ? t(" · 这一场之后") : ""}`, [
            { value: usd(bar.open), name: t("开盘") }, { value: usd(bar.high), name: t("最高") }, { value: usd(bar.low), name: t("最低") },
            { value: usd(bar.close), name: t("收盘") }, { value: num(bar.volume), name: t("成交量（股）") },
          ]);
        hair.setAttribute("x1", bar.x);
        hair.setAttribute("x2", bar.x);
        hair.setAttribute("visibility", "visible");
        if (onBar) onBar(drawn.candles, at);
      });
      strip.addEventListener("pointerleave", () => {
        hideTip();
        hair.setAttribute("visibility", "hidden");
        if (onBar) onBar(drawn.candles, before.length - 1);            // back to the session of the meeting
      });
      frame.append(strip);
    });
    if (onBar) onBar(drawn.candles, before.length - 1);
    // And one mark per horizon of the forecast, to point at for its recorded percentiles.
    for (const point of drawn.fan ? drawn.fan.points : []) {
      const mark = svg("circle", { class: "fan-dot", cx: point.x, cy: point.y50, r: 3.2, tabindex: 0, "aria-label": t`预测，${fanText(point)}` });
      const tip = (px, py) => showTip(px, py, t`${ticker} · 预测 · ${point.sessions} 个交易日后`, [
        { value: usd(point["95"]), name: t("95% 的路径低于") }, { value: usd(point["75"]), name: "75%" },
        { value: usd(point["50"]), name: t("中位数") }, { value: usd(point["25"]), name: "25%" },
        { value: usd(point["5"]), name: t("5% 的路径低于") },
      ]);
      mark.addEventListener("pointermove", (pointer) => tip(pointer.clientX, pointer.clientY));
      mark.addEventListener("pointerleave", hideTip);
      mark.addEventListener("focus", () => {
        const box = mark.getBoundingClientRect();
        tip(box.left + box.width / 2, box.top);
      });
      mark.addEventListener("blur", hideTip);
      frame.append(mark);
    }
    return frame;
  }

  function showTicker(ticker, context) {
    const item = state.market.tickers[ticker];
    if (!item || !item.price || item.bars.dates.length === 0) {
      head.append(el("span", { class: "holo-tag", text: ticker }), el("span", { class: "holo-name", text: t("这只股票不在这一场的行情快照里") }));
      body.append(el("p", { class: "holo-empty", text: t("这一场的候选和持仓里没有它（多半是之前的订单在今天成交或撤回），所以没有它的 K 线可看。") }));
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
    const fan = fanOf(ticker, context.events, context.index);
    const ahead = fan ? fan.bands[fan.bands.length - 1].sessions : 0;
    const before = lastBars(item.bars, BARS_ON_SCREEN - Math.max(later.length, ahead));
    // A level too far from the candles to be drawn among them is named under the chart instead.
    const far = plot(before, later, levels, SIZE).levels.filter((level) => level.off);
    const plotBox = el("div", { class: "holo-plot" });
    // Over the chart's upper edge, the session in figures: the meeting's own until a day is pointed at.
    const legend = el("div", { class: "holo-legend" });
    body.append(legend, plotBox);
    foot.append(
      el("div", { class: "reads" }, readouts(item).slice(2).map((part) =>
        el("span", { class: "read" }, el("small", { text: part.label }), el("b", { class: part.lean === "flat" ? null : part.lean, text: part.text })))),
      el("div", { class: "holo-notes" },
        far.map((level) => el("span", { class: `note-level ${level.kind}`, text: levelText(level) })),
        fan ? el("span", { class: "note-fan", text: t("扇形是量化工程师（代码）记录的预测：一种分布，不是会走出来的路线。") }) : null,
        since ? el("span", { class: `note-since ${since.lean}`, text: sinceText(since) }) : null,
        context.ended && !since ? el("span", { class: "muted", text: t("这一场之后还没有新的交易日记录。") }) : null));
    // Drawn last, at the size the screen leaves for it once everything round it is in place.
    const wide = plotBox.clientWidth, tall = plotBox.clientHeight;
    plotBox.append(candleChart(ticker, before, later, levels, gauges(item), wide > 40 && tall > 40 ? { width: wide, height: tall } : SIZE, fan,
      (bars, at) => fill(legend, ...legendOf(bars, at))));
  }

  /** The tickers on the portfolio manager's list, side by side, each with what was proposed for it. */
  function showList(tickers, context) {
    head.append(el("span", { class: "holo-tag", text: t("提案") }), el("span", { class: "holo-name", text: t("组合经理这份清单上的股票") }));
    if (tickers.length === 0) {
      body.append(el("p", { class: "holo-empty", text: t("这份清单上没有任何股票。") }));
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
        item && item.price ? el("div", { class: "mini-price" }, usd(item.price.close), " ", move(day)) : el("div", { class: "muted", text: t("快照里没有它") }));
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
    foot.append(el("div", { class: "holo-notes muted", text: t("记录里和这个领域有关的数据。每位宏观分析师拿到的是同一张完整的数据表。") }));
  }

  function showOverview() {
    head.append(el("span", { class: "holo-tag", text: t("大盘") }), el("span", { class: "holo-name", text: t("这一场用的行情快照") }));
    // The tiles first; under them the yield curve beside the sector funds, so all three fit the screen at once.
    const curve = curveChart(state.market), strip = sectorStrip(state.market);
    body.append(el("div", { class: "read-grid" }, headline(state.market).map(tile)),
      el("div", { class: "holo-pair" }, ...[curve, strip].filter(Boolean)));
    foot.append(el("div", { class: "holo-notes muted", text: t("实线是记录的日线；虚线只连着四个记录值。") }));
  }

  function paint() {
    const context = state.context;
    clear(head);
    clear(body);
    clear(foot);
    hideTip();
    if (!state.market || !state.market.available) {
      head.append(el("span", { class: "holo-tag", text: t("行情") }));
      body.append(el("p", { class: "holo-empty", text: state.market ? WHY[state.market.reason] || WHY.no_snapshot : t("正在读这一场的行情快照。") }));
      return;
    }
    // Put in first and shown last (the style sheet orders it), so a chart is measured with everything round it in place.
    foot.append(el("div", { class: "holo-asof", text: t`截至 ${state.market.session} 收盘 · 日线` }));
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
      const steps = row ? STEPS.map((name) => (reached(row[name], context.index) ? stepText(name, row[name], { upTo: context.index }) : "")).join("|") : "";
      // A forecast said for a ticker changes its chart too: the line it was said at is part of what is shown.
      const fan = focus.kind === "ticker" ? fanOf(ticker, context.events || [], context.index) : null;
      return `${steps}#${fan ? fan.index : ""}`;
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
