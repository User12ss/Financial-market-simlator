// The dashboard's charts, drawn as plain SVG at the width they are given on
// the page, so text keeps its size on a narrow window. Each one comes with a
// legend when it has more than one kind of mark, a tooltip that keyboard
// focus can reach too, and a table of the same numbers, so no value is
// hover-only and nothing is told by colour alone.

import { el, svg, clear, showTip, hideTip, table } from "./dom.js";
import { t } from "./i18n.js";

const css = (name) => `var(${name})`;
const given = (value) => value !== null && value !== undefined;

/** Round tick values covering [low, high]. */
export function ticks(low, high, wanted = 4) {
  if (low === high) {
    const pad = Math.abs(low) * 0.01 || 1;
    low -= pad;
    high += pad;
  }
  const rough = (high - low) / wanted;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * power).find((size) => size >= rough);
  const found = [];
  for (let value = Math.floor(low / step) * step; value <= high + step * 1e-9; value += step) {
    found.push(Math.round(value / step) * step);
  }
  if (found[found.length - 1] < high) found.push(found[found.length - 1] + step);
  return found;
}

/** Draws at the width the page gives it, and again whenever that width changes. */
function fitted(draw) {
  const holder = el("div", { class: "plot" });
  let width = 0;
  new ResizeObserver((entries) => {
    const now = Math.round(entries[0].contentRect.width);
    if (now === 0 || now === width) return;
    width = now;
    hideTip();
    clear(holder).append(draw(Math.max(320, width)));
  }).observe(holder);
  return holder;
}

function legend(keys) {
  return el("div", { class: "legend" }, keys.map((key) =>
    el("span", { class: "key" },
      el("span", { class: `swatch-${key.mark || "line"}`, style: `background:${key.color}` }),
      key.name)));
}

function tableTwin(node) {
  return el("details", {}, el("summary", { text: t("查看数据表") }), node);
}

/** Where a point in the chart's own units sits on the screen. */
function onScreen(frame, x, y) {
  const box = frame.getBoundingClientRect();
  const view = frame.viewBox.baseVal;
  return [box.left + (x / view.width) * box.width, box.top + (y / view.height) * box.height];
}

/**
 * Lines over sessions, one per series, all on one axis.
 * series: [{ name, color, points: [{ d: "2026-11-16", v: 99267.78 }] }]
 */
export function lineChart({ series, valueText, label }) {
  const shown = series.filter((item) => item.points.length > 0);
  const days = [...new Set(shown.flatMap((item) => item.points.map((point) => point.d)))].sort();
  if (days.length === 0) return el("p", { class: "empty", text: t("还没有可以画的净值记录。") });
  const values = shown.flatMap((item) => item.points.map((point) => point.v));
  const marks = ticks(Math.min(...values), Math.max(...values));
  const low = marks[0], high = marks[marks.length - 1];
  const lookup = shown.map((item) => new Map(item.points.map((point) => [point.d, point.v])));

  function draw(W) {
    const H = 280, left = 66, right = 118, top = 14, bottom = 30;
    const x = (index) => (days.length === 1 ? (left + W - right) / 2 : left + (index / (days.length - 1)) * (W - left - right));
    const y = (value) => top + (1 - (value - low) / (high - low)) * (H - top - bottom);
    const frame = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", tabindex: 0, "aria-label": label });
    for (const mark of marks) {
      frame.append(
        svg("line", { class: "grid-line", x1: left, x2: W - right, y1: y(mark), y2: y(mark) }),
        svg("text", { x: left - 8, y: y(mark) + 4, "text-anchor": "end", text: valueText(mark, true) }));
    }
    frame.append(svg("line", { class: "axis-line", x1: left, x2: W - right, y1: H - bottom, y2: H - bottom }));
    const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor((W - left - right) / 70))));
    days.forEach((day, index) => {
      if (index % every === 0 || index === days.length - 1) {
        frame.append(svg("text", { x: x(index), y: H - bottom + 18, "text-anchor": "middle", text: day.slice(5) }));
      }
    });

    // The lines, then a ringed marker and a name at each line's end.
    const ends = shown.map((item) => {
      const path = item.points.map((point, index) =>
        `${index === 0 ? "M" : "L"}${x(days.indexOf(point.d)).toFixed(1)},${y(point.v).toFixed(1)}`).join(" ");
      // The path is told its length is 1, so that it can be drawn in from its start whatever its real length.
      frame.append(svg("path", { class: "line", d: path, stroke: item.color, pathLength: 1 }));
      const last = item.points[item.points.length - 1];
      return { name: item.name, color: item.color, x: x(days.indexOf(last.d)), y: y(last.v), text: valueText(last.v) };
    });
    ends.sort((a, b) => a.y - b.y);
    let floor = -Infinity;
    for (const end of ends) {                     // keep the end labels from sitting on each other
      end.labelY = Math.max(end.y, floor + 32);
      floor = end.labelY;
    }
    const overflow = floor - (H - bottom - 14);
    if (overflow > 0) for (const end of ends) end.labelY -= overflow;
    for (const end of ends) {
      frame.append(
        svg("circle", { class: "dot end", cx: end.x, cy: end.y, r: 5, fill: end.color }),
        svg("text", { class: "label end", x: end.x + 12, y: end.labelY - 1, text: end.name }),
        svg("text", { class: "strong end", x: end.x + 12, y: end.labelY + 13, text: end.text }));
    }

    // One crosshair for every series: the reader aims at a day, not at a line.
    const hair = svg("line", { class: "crosshair", y1: top, y2: H - bottom, visibility: "hidden" });
    const dots = shown.map((item) => svg("circle", { class: "dot", r: 5, fill: item.color, visibility: "hidden" }));
    frame.append(hair, ...dots);
    let current = null;
    function point(index) {
      current = index;
      hair.setAttribute("x1", x(index));
      hair.setAttribute("x2", x(index));
      hair.setAttribute("visibility", "visible");
      const rows = [];
      shown.forEach((item, at) => {
        const value = lookup[at].get(days[index]);
        dots[at].setAttribute("visibility", value === undefined ? "hidden" : "visible");
        if (value === undefined) return;
        dots[at].setAttribute("cx", x(index));
        dots[at].setAttribute("cy", y(value));
        rows.push({ color: item.color, value: valueText(value), name: item.name });
      });
      showTip(...onScreen(frame, x(index), top + 20), days[index], rows);
    }
    function leave() {
      current = null;
      hair.setAttribute("visibility", "hidden");
      dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
      hideTip();
    }
    const hit = svg("rect", { class: "hit", x: left - 10, y: top, width: W - left - right + 20, height: H - top - bottom });
    hit.addEventListener("pointermove", (event) => {
      const box = frame.getBoundingClientRect();
      const at = ((event.clientX - box.left) / box.width) * W;
      let nearest = 0;
      days.forEach((_, index) => { if (Math.abs(x(index) - at) < Math.abs(x(nearest) - at)) nearest = index; });
      if (nearest !== current) point(nearest);
    });
    hit.addEventListener("pointerleave", leave);
    frame.append(hit);
    frame.addEventListener("focus", () => point(days.length - 1));
    frame.addEventListener("blur", leave);
    frame.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const from = current === null ? days.length - 1 : current;
        point(Math.min(days.length - 1, Math.max(0, from + (event.key === "ArrowRight" ? 1 : -1))));
      } else if (event.key === "Escape") leave();
    });
    return frame;
  }

  const columns = [{ title: t("交易日"), cell: (day) => day }].concat(shown.map((item, at) => ({
    title: item.name, num: true, cell: (day) => (lookup[at].has(day) ? valueText(lookup[at].get(day)) : null),
  })));
  return el("div", { class: "chart" },
    shown.length > 1 ? legend(shown.map((item) => ({ name: item.name, color: item.color }))) : null,
    fitted(draw), tableTwin(table(columns, days.slice().reverse())));
}

/**
 * Horizontal bars on a 0–100% scale, each with an optional tick for a second
 * figure of the same kind (what was claimed, beside what happened).
 * rows: [{ label, value, marker, note }] with value and marker as fractions.
 */
export function rateBars({ rows, barName, markerName, label }) {
  function draw(W) {
    const left = 112, right = 150, top = 8, lane = 34, thick = 16;
    const H = top + rows.length * lane + 26;
    const x = (fraction) => left + fraction * (W - left - right);
    const frame = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": label });
    for (const mark of [0, 0.25, 0.5, 0.75, 1]) {
      frame.append(
        svg("line", { class: mark === 0 ? "axis-line" : "grid-line", x1: x(mark), x2: x(mark), y1: top, y2: H - 26 }),
        svg("text", { x: x(mark), y: H - 8, "text-anchor": "middle", text: `${mark * 100}%` }));
    }
    rows.forEach((row, index) => {
      const mid = top + index * lane + lane / 2;
      const width = Math.max(0, x(row.value || 0) - x(0));
      const round = Math.min(4, width);
      const shape = `M${x(0)},${mid - thick / 2} h${width - round} q${round},0 ${round},${round} v${thick - 2 * round} ` +
        `q0,${round} ${-round},${round} h${-(width - round)} z`;
      const text = given(row.value) ? `${Math.round(row.value * 100)}%` : t("没有数据");
      const tipRows = [{ color: css("--series-1"), value: text, name: barName }];
      if (given(row.marker)) tipRows.push({ color: css("--ink"), value: `${Math.round(row.marker * 100)}%`, name: markerName });
      if (row.note) tipRows.push({ value: row.note, name: "" });
      const group = svg("g", { class: "bar", tabindex: 0, "aria-label": `${row.label}：${barName} ${text}。${row.note || ""}` },
        svg("text", { class: "label", x: left - 10, y: mid + 4, "text-anchor": "end", text: row.label }),
        svg("path", { d: shape, fill: css("--series-1") }),
        given(row.marker)
          ? svg("rect", { x: x(row.marker) - 1, y: mid - thick / 2 - 4, width: 2, height: thick + 8, fill: css("--ink") }) : null,
        svg("text", { class: "strong", x: Math.max(x(row.value || 0), given(row.marker) ? x(row.marker) : 0) + 8, y: mid + 4, text }),
        svg("text", { x: W - 4, y: mid + 4, "text-anchor": "end", text: row.note || "" }),
        svg("rect", { class: "hit", x: 0, y: mid - lane / 2, width: W, height: lane }));
      group.addEventListener("pointermove", (event) => showTip(event.clientX, event.clientY, row.label, tipRows));
      group.addEventListener("pointerleave", hideTip);
      group.addEventListener("focus", () => showTip(...onScreen(frame, x(row.value || 0), mid), row.label, tipRows));
      group.addEventListener("blur", hideTip);
      frame.append(group);
    });
    return frame;
  }
  return el("div", { class: "chart" },
    legend([{ name: barName, color: css("--series-1"), mark: "box" }, { name: markerName, color: css("--ink"), mark: "tick" }]),
    fitted(draw));
}

/**
 * Where one value falls among many: the middle half as a band, the 5%–95%
 * range as a whisker, the median as a tick, and the value itself as a dot.
 */
export function quantileStrip({ quantiles, value, valueName, valueText, label }) {
  const all = [quantiles["5"], quantiles["95"], value].filter(given);
  const marks = ticks(Math.min(...all), Math.max(...all), 5);
  const low = marks[0], high = marks[marks.length - 1];
  const rows = [
    { color: css("--series-1"), value: valueText(value), name: valueName },
    { color: css("--ink"), value: valueText(quantiles["50"]), name: t("随机结果的中位数") },
    { color: css("--band"), value: t`${valueText(quantiles["25"])} 到 ${valueText(quantiles["75"])}`, name: t("中间一半") },
    { color: css("--muted"), value: t`${valueText(quantiles["5"])} 到 ${valueText(quantiles["95"])}`, name: t("5% 到 95%") },
  ];

  function draw(W) {
    const H = 112, left = 34, right = 34, mid = 50;
    const x = (amount) => left + ((amount - low) / (high - low)) * (W - left - right);
    const frame = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", tabindex: 0, "aria-label": label });
    frame.append(svg("line", { class: "axis-line", x1: left, x2: W - right, y1: H - 30, y2: H - 30 }));
    const every = Math.ceil(marks.length / Math.max(2, Math.floor((W - left - right) / 80)));
    marks.forEach((mark, index) => {
      frame.append(svg("line", { class: "grid-line", x1: x(mark), x2: x(mark), y1: 22, y2: H - 30 }));
      if (index % every === 0) frame.append(svg("text", { x: x(mark), y: H - 12, "text-anchor": "middle", text: valueText(mark, true) }));
    });
    frame.append(
      svg("line", { x1: x(quantiles["5"]), x2: x(quantiles["95"]), y1: mid, y2: mid, stroke: css("--muted"), "stroke-width": 2 }),
      svg("rect", {
        x: x(quantiles["25"]), y: mid - 10, width: Math.max(2, x(quantiles["75"]) - x(quantiles["25"])), height: 20, rx: 4,
        fill: css("--band"),
      }),
      svg("rect", { x: x(quantiles["50"]) - 1, y: mid - 14, width: 2, height: 28, fill: css("--ink") }));
    if (given(value)) {
      const flip = x(value) > W / 2;
      frame.append(
        svg("circle", { class: "dot", cx: x(value), cy: mid, r: 6, fill: css("--series-1") }),
        svg("text", { class: "strong", x: x(value) + (flip ? 8 : -8), y: 16, "text-anchor": flip ? "end" : "start",
          text: `${valueName} ${valueText(value)}` }));
    }
    const hit = svg("rect", { class: "hit", x: 0, y: 0, width: W, height: H - 30 });
    hit.addEventListener("pointermove", (event) => showTip(event.clientX, event.clientY, null, rows));
    hit.addEventListener("pointerleave", hideTip);
    frame.append(hit);
    frame.addEventListener("focus", () => showTip(...onScreen(frame, x(given(value) ? value : quantiles["50"]), mid), null, rows));
    frame.addEventListener("blur", hideTip);
    return frame;
  }

  const twin = table(
    [{ title: t("位置"), cell: (row) => row.name }, { title: t("盈亏"), num: true, cell: (row) => row.text }],
    [
      { name: valueName, text: valueText(value) },
      ...["5", "25", "50", "75", "95"].map((key) => ({ name: t`随机结果的 ${key}% 分位`, text: valueText(quantiles[key]) })),
    ]);
  return el("div", { class: "chart" },
    legend([
      { name: valueName, color: css("--series-1"), mark: "dot" },
      { name: t("随机结果的中间一半（25%–75%）"), color: css("--band"), mark: "box" },
      { name: t("5%–95% 的范围"), color: css("--muted"), mark: "line" },
      { name: t("中位数"), color: css("--ink"), mark: "tick" },
    ]),
    fitted(draw), tableTwin(twin));
}

/** How far a figure has gone towards a limit. The words beside it say the state as well. */
export function meter({ name, valueText, limitText, share, tripped }) {
  return el("div", { class: tripped ? "meter tripped" : "meter" },
    el("div", { class: "row spread" },
      el("span", { text: name }),
      el("span", { class: "small muted", text: t`${valueText}，上限 ${limitText}` })),
    el("div", { class: "bar", role: "img", "aria-label": t`${name}：${valueText}，上限 ${limitText}` },
      el("div", { class: "fill", style: `width:${Math.round(Math.min(1, Math.max(0, share)) * 100)}%` })));
}
