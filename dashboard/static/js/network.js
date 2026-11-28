// Who has spoken to whom, as a small map beside the room. The figures stand
// where they stand round the table; a line joins two of them once the record
// has one of them speaking to the other, heavier the more lines there have
// been, and the line being played is lit and moving. Speakers with no seat
// (the pipeline's own voice) stand in the middle.
// It is counted off the record up to the line being played, and adds nothing.

import { el, svg, clear, showTip, hideTip } from "./dom.js";
import { seats } from "./figures.js";
import { EVERYONE, exchanges } from "./links.js";
import { english, t } from "./i18n.js";

const BOX = { width: 320, height: 212, cx: 160, cy: 104, rx: 118, ry: 70 };
const PAIRS_LISTED = 4;

/** A title short enough to stand beside a dot: the job without its "分析师". */
const BRIEF = {
  "Portfolio Manager": "PM", "Ticker Analyst": "Ticker", "Central Bank": "Cen. Bank", "Fiscal Policy": "Fiscal", "Equity Market": "Equity",
  "Fixed Income": "Rates", "Commodities & Inflation": "Cmdty", "Risk Officer": "Risk", "Scenario Analyst": "Scenario",
  "Quant Engineer": "Quant", "News Desk": "News",
};
export const shortTitle = (title) => (english ? BRIEF[title] || title : title.replace(/分析师$/, "").replace(/与.*$/, "") || title);

/** A curve between two points that leans towards the middle of the map; `side` moves it off its twin the other way. */
function chord(from, to, side = 0) {
  const mid = [(from.x + to.x) / 2, (from.y + to.y) / 2];
  const length = Math.hypot(to.x - from.x, to.y - from.y) || 1;
  const normal = [-(to.y - from.y) / length, (to.x - from.x) / length];
  const pull = 0.45;
  const cx = mid[0] + (BOX.cx - mid[0]) * pull + normal[0] * side * 7;
  const cy = mid[1] + (BOX.cy - mid[1]) * pull + normal[1] * side * 7;
  return `M${from.x.toFixed(1)},${from.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${to.x.toFixed(1)},${to.y.toFixed(1)}`;
}

/**
 * The map. `update(events, index)` redraws it for the line being played;
 * `onPick(id)` is called when a seated figure's dot is chosen, `onSeek(line)`
 * with the latest line of a pair picked from the list under the map.
 */
export function talkNetwork({ roster, onPick, onSeek }) {
  const places = seats(roster.length, BOX);
  const seated = new Map(roster.map((persona) => [persona.id, { ...places[persona.seat], persona, title: persona.title }]));
  const frame = svg("svg", { class: "net", viewBox: `0 0 ${BOX.width} ${BOX.height}`, role: "img" });
  const list = el("ol", { class: "net-list" });
  const node = el("section", { class: "card network" },
    el("h2", { text: t("谁在对谁说") }),
    el("p", { class: "sub", text: t("到这一句为止。线越粗，说得越多；亮着的是正在说的这一句。") }),
    frame, list);
  let drawn = null;

  function update(events, index) {
    const event = index >= 0 && index < events.length ? events[index] : null;
    const key = `${events.length}:${index}`;
    if (key === drawn) return;
    drawn = key;
    const tally = exchanges(events, index);
    // Whoever has spoken or been spoken to without a seat stands in the middle, in order of first appearance.
    const guests = new Map();
    events.slice(0, index + 1).forEach((line) => {
      for (const [id, title] of [[line.speaker, line.speaker_title], [line.to, line.to_title]]) {
        if (id && id !== EVERYONE && !seated.has(id) && !guests.has(id)) guests.set(id, title || id);
      }
    });
    const spots = new Map(seated);
    [...guests].forEach(([id, title], at) => {
      const offset = (at - (guests.size - 1) / 2) * 56;
      // Side by side their names would run into each other: every other one is written above its mark.
      spots.set(id, { x: BOX.cx + offset, y: BOX.cy, title, persona: null, side: 0, above: at % 2 === 1 });
    });
    const now = event && event.to && event.to !== event.speaker ? { from: event.speaker, to: event.to } : null;

    clear(frame);
    hideTip();
    frame.setAttribute("aria-label", tally.pairs.length
      ? t`到这一句为止，${tally.pairs.slice(0, PAIRS_LISTED).map((pair) => t`${spots.get(pair.from).title}对${spots.get(pair.to).title}说了 ${pair.count} 句`).join("，")}`
      : t("到这一句为止，还没有谁对谁单独说过话"));
    frame.append(svg("ellipse", { class: "net-table", cx: BOX.cx, cy: BOX.cy, rx: BOX.rx - 22, ry: BOX.ry - 16 }));
    const twins = new Set(tally.pairs.map((pair) => `${pair.from}>${pair.to}`));
    for (const pair of [...tally.pairs].reverse()) {                   // the heaviest last, so it lies on top
      const from = spots.get(pair.from), to = spots.get(pair.to);
      if (!from || !to) continue;
      const lit = now && now.from === pair.from && now.to === pair.to;
      const side = twins.has(`${pair.to}>${pair.from}`) ? 1 : 0;
      const path = svg("path", {
        class: lit ? "net-link now" : "net-link", d: chord(from, to, side),
        style: `--w: ${Math.min(5, 0.8 + 0.9 * Math.sqrt(pair.count)).toFixed(2)}px; --tone: var(--tone-${from.persona ? from.persona.tone : 6})`,
      });
      frame.append(path);
      if (lit) frame.append(svg("path", { class: "net-flow", d: chord(from, to, side) }));
    }
    for (const [id, spot] of spots) {
      const calls = tally.calls.get(id) || 0, said = tally.said.get(id) || 0;
      const speaking = event && event.speaker === id, hearing = now && now.to === id;
      const everyone = speaking && event.to === EVERYONE;
      const group = svg("g", {
        class: `net-node${spot.persona ? "" : " guest"}${speaking ? " speaking" : ""}${hearing ? " hearing" : ""}${said ? "" : " silent"}`,
        transform: `translate(${spot.x.toFixed(1)},${spot.y.toFixed(1)})`,
        style: `--tone: var(--tone-${spot.persona ? spot.persona.tone : 6})`,
      });
      if (calls) group.append(svg("circle", { class: "net-calls", r: 7.5 + Math.min(5, calls) }));
      if (everyone) group.append(svg("circle", { class: "net-wave", r: 8 }));
      group.append(spot.persona ? svg("circle", { class: "net-dot", r: 5.5 }) : svg("rect", { class: "net-dot", x: -5, y: -5, width: 10, height: 10, rx: 2 }));
      // A name goes outside the ring of dots: to the side at the two ends, above at the back, below at the front.
      const up = spot.y < BOX.cy - 4;
      const label = spot.persona
        ? (spot.side ? { x: spot.side * 10, y: 3.5, anchor: spot.side > 0 ? "start" : "end" } : { x: 0, y: up ? -10 : 16.5, anchor: "middle" })
        : { x: 0, y: spot.above ? -10 : 17, anchor: "middle" };
      group.append(svg("text", { class: "net-name", x: label.x, y: label.y, "text-anchor": label.anchor, text: shortTitle(spot.title) }));
      const hit = svg("circle", { class: "net-hit", r: 12 });
      hit.addEventListener("pointermove", (pointer) => {
        const with_ = tally.pairs.filter((pair) => pair.from === id || pair.to === id).slice(0, 4);
        showTip(pointer.clientX, pointer.clientY, spot.title, [
          { value: t`${said} 句`, name: t("到这一句为止说了") },
          ...(calls ? [{ value: t`${calls} 句`, name: t("其中对所有人说的") }] : []),
          ...with_.map((pair) => ({ value: t`${pair.count} 句`, name: `${spots.get(pair.from).title} → ${spots.get(pair.to).title}` })),
        ]);
      });
      hit.addEventListener("pointerleave", hideTip);
      if (spot.persona) hit.addEventListener("click", () => onPick(id));
      group.append(hit);
      frame.append(group);
    }

    clear(list);
    const top = tally.pairs.slice(0, PAIRS_LISTED);
    if (top.length === 0) {
      list.append(el("li", { class: "muted", text: events.length ? t("还没有谁对谁单独说过话。") : t("还没有记录。") }));
    }
    for (const pair of top) {
      const lit = now && now.from === pair.from && now.to === pair.to;
      list.append(el("li", {}, el("button", {
        type: "button", class: lit ? "net-pair now" : "net-pair", title: t("跳到这两个人之间最近的一句"), onclick: () => onSeek(pair.last),
      },
        el("span", { text: spots.get(pair.from).title }), el("i", { "aria-hidden": "true", text: "→" }),
        el("span", { text: spots.get(pair.to).title }), el("b", { text: t`${pair.count} 句` }))));
    }
  }

  return { node, update };
}
