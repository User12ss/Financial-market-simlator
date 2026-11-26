// The tape along the top of the page: every ticker of the latest session's
// snapshot with its recorded close, its day's move, a small line of its recent
// closes and how much it swings, then the market series. It runs by itself,
// stops under the pointer and can be stopped with its button; a reader who
// asked for less motion gets it standing still, to scroll by hand. What it
// shows is worked out in market.js.

import { el, svg, clear } from "./dom.js";
import { sparkPath, tape } from "./market.js";
import { t } from "./i18n.js";

const ARROW = { up: "▲ ", down: "▼ ", flat: "" };

function entry(item) {
  const line = item.spark ? sparkPath(item.spark, 44, 14) : null;
  return el("span", { class: "tape-item" },
    el("b", { text: item.name }),
    line ? svg("svg", { class: `spark ${item.lean}`, viewBox: "0 0 44 14", "aria-hidden": "true" }, svg("path", { d: line })) : null,
    el("span", { class: "tape-value", text: item.value }),
    el("span", { class: item.lean === "flat" ? "tape-move" : `tape-move ${item.lean}`, text: `${ARROW[item.lean]}${item.move}` }),
    item.swing ? el("span", { class: "tape-swing", title: t("日均波幅：ATR 占收盘价的比例"), text: t`波幅 ${item.swing}` }) : null);
}

export function tickerTape() {
  const track = el("div", { class: "tape-track" });
  const note = el("span", { class: "tape-note" });
  // Moving text has to be stoppable without a pointer resting on it: a button does it for keyboard and touch.
  const pause = el("button", {
    type: "button", class: "tape-pause", "aria-pressed": "false", "aria-label": t("让行情带停住"), text: "⏸",
    onclick: () => {
      const stopped = !track.classList.contains("paused");
      track.classList.toggle("paused", stopped);
      pause.setAttribute("aria-pressed", String(stopped));
      pause.textContent = stopped ? "▶" : "⏸";
    },
  });
  const node = el("div", { class: "tape", hidden: true, role: "region", "aria-label": t("行情带") },
    note, pause, el("div", { class: "tape-window" }, track));
  let shown = null;

  /** Take the latest session's market data (as /api/market answers; with no snapshot it lists nothing), or nothing at all. */
  function load(market) {
    const items = market ? tape(market) : [];
    const key = JSON.stringify([market ? market.run_id : null, items]);
    if (key === shown) return;                          // the same tape as before: let it run on
    shown = key;
    node.hidden = items.length === 0;
    clear(track);
    if (items.length === 0) return;
    note.textContent = t`${market.session} 收盘`;
    // Twice over, so the strip can run round without a gap. The second copy is not read out.
    track.append(el("div", { class: "tape-run" }, items.map(entry)),
      el("div", { class: "tape-run", "aria-hidden": "true" }, items.map(entry)));
    track.style.setProperty("--tape-seconds", `${Math.max(30, items.length * 4)}s`);
  }

  return { node, load };
}
