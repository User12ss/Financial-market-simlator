// The decision trail: one row per ticker, one cell per step it went through,
// appearing as the record is played. It answers "how did this order come
// about" at a glance, and every cell leads back to the line that was said.
// What the cells say is worked out in decisions.js from the record alone, and
// only from the lines played so far. A ticker nobody has mentioned yet has no
// row on the page, and a step that has not happened yet is an empty place:
// no words, no arrow, no label and no tooltip, whether or not the record ever
// gets to it.

import { el, clear, showTip, hideTip, word, signedPct } from "./dom.js";
import {
  STEPS, buildTrail, firstLine, funnel, isLine, lean, lineAt, outcome, outcomeText, reached, stepText,
} from "./decisions.js";
import { replay } from "./motion.js";

const NAMES = { view: "观点", debate: "辩论", ruling: "裁决", proposal: "提案", check: "检查", risk: "风控", order: "下单" };
const WHO = {
  view: "标的分析师", debate: "多方和空方", ruling: "裁判", proposal: "组合经理", check: "风控官（规则）",
  risk: "风控官（规则）", order: "交易员（规则）",
};
const GLYPH = { up: "▲", down: "▼", stop: "✕", flat: "" };
const FUNNEL = [
  ["views", "观点"], ["ruled", "辩论后裁决"], ["proposed", "要动的提案"], ["accepted", "通过检查"],
  ["sized", "风控放行"], ["ordered", "下单"],
];

/** How many tickers got how far, as a row of steps that narrows. `set(counts)` moves it. */
export function funnelBar() {
  const steps = FUNNEL.map(([key, label]) => {
    const count = el("b", { text: "0" });
    const fill = el("i", {});
    return { key, count, fill, node: el("div", { class: "funnel-step" }, count, el("span", { text: label }), el("div", { class: "funnel-bar" }, fill)) };
  });
  const kept = el("span", { class: "chip chip-quiet" });
  const back = el("span", { class: "chip chip-quiet" });
  const node = el("div", { class: "funnel" },
    steps.flatMap((step, at) => (at ? [el("span", { class: "funnel-arrow", "aria-hidden": "true", text: "→" }), step.node] : [step.node])),
    el("div", { class: "funnel-notes" }, kept, back));
  let shown = null;
  function set(counts) {
    const widest = Math.max(1, counts.views, counts.proposed);
    for (const step of steps) {
      const value = counts[step.key];
      if (!shown || shown[step.key] !== value) {
        step.count.textContent = String(value);
        if (shown) replay(step.count, "bump");
      }
      step.fill.style.width = `${Math.round((value / widest) * 100)}%`;
      step.node.classList.toggle("empty-step", value === 0);
    }
    kept.textContent = `继续持有 ${counts.kept}`;
    back.textContent = `被退回或拒绝 ${counts.turned_back}`;
    shown = { ...counts };
  }
  return { node, set };
}

/**
 * The trail of one session. `load(events, run, live)` takes the record,
 * `update(index)` shows how far things had got by that line. `onSeek(line)`
 * is called with the place of the line a cell stands for. With `whole`, the
 * trail always shows the record played to its end, and has no button to
 * choose between that and the line being played.
 */
export function decisionTrail({
  onSeek, whole: always = false,
  lead = "每只股票一行，从左到右是它走过的每一步，随播放一格一格出现。点任一格，回到那一句原话。",
}) {
  const state = { events: [], trail: [], rows: [], index: -1, whole: always, live: false, run: null, count: -1 };
  const bar = funnelBar();
  const whole = always ? null : el("button", {
    type: "button", class: "chip", "aria-pressed": "false", text: "直接看最终结果",
    onclick: () => {
      state.whole = !state.whole;
      whole.setAttribute("aria-pressed", String(state.whole));
      update(state.index);
    },
  });
  const head = el("div", { class: "trail-row trail-head", hidden: true },
    el("span", { text: "标的" }),
    el("div", { class: "trail-steps" }, STEPS.map((name) => el("span", { text: NAMES[name], title: WHO[name] }))),
    el("span", { text: "走到了哪一步" }));
  const body = el("div", { class: "trail-body" }, head);
  const none = el("p", { class: "empty", hidden: true });
  const aside = el("p", { class: "small muted" });
  const node = el("section", { class: "card trail" },
    el("div", { class: "row spread" }, el("h2", { text: "这一场的决策是怎么做出来的" }), whole),
    el("p", { class: "sub", text: lead }),
    bar.node, el("div", { class: "scroll" }, body), none, aside);

  /** How far the trail may speak of: the whole record, or the lines played so far. */
  const horizon = () => (state.whole ? Infinity : state.index);

  /** A step's place in its row. It starts empty and out of reach, and stays so for a step the record never gets to. */
  function cell(item, name, at) {
    const step = () => item.row[name];                   // read anew each time: a run that is going adds to its record
    const glyph = el("i", { "aria-hidden": "true" });
    const text = el("span", {});
    const button = el("button", {
      type: "button", class: "node", hidden: true,
      onclick: () => { if (reached(step(), horizon())) onSeek(lineAt(name, step(), horizon())); },
    }, glyph, text);
    button.style.gridColumn = String(at + 1);            // its own column, so an empty place stays empty instead of closing the gap
    const tip = (x, y) => {
      if (!reached(step(), horizon())) return;          // not said yet: nothing to tell
      const said = state.events[lineAt(name, step(), horizon())];    // the step's latest line so far
      showTip(x, y, `${item.row.ticker} · ${NAMES[name]} · ${WHO[name]}`, [
        { value: stepText(name, step(), { upTo: horizon() }), name: "" },
        ...(said ? [{ value: said.remark, name: "", plain: true }] : []),
      ]);
    };
    button.addEventListener("pointermove", (event) => tip(event.clientX, event.clientY));
    button.addEventListener("pointerleave", hideTip);
    button.addEventListener("focus", () => {
      const box = button.getBoundingClientRect();
      tip(box.left, box.bottom);
    });
    button.addEventListener("blur", hideTip);
    return { name, node: button, glyph, text, said: "" };
  }

  /**
   * Put a ticker's row on the page, now that the record has got to it. The rows are kept in the order the record
   * gets to them, so the ones on the page are always the first few and a new one goes after them.
   */
  function open(item) {
    item.cells = STEPS.map((name, at) => cell(item, name, at));
    item.lit = el("i", { class: "rail-lit" });
    item.result = el("span", { class: "trail-result" });
    item.held = el("span", { class: "chip chip-quiet", text: "持仓", hidden: true });
    item.again = el("span", { class: "trail-note", hidden: true });
    item.said = null;
    item.root = el("div", { class: "trail-row" },
      el("span", { class: "trail-ticker" }, el("strong", { text: item.row.ticker }), item.held),
      el("div", { class: "trail-steps" }, item.lit, item.cells.map((part) => part.node)),
      el("div", { class: "trail-end" }, item.result, item.again));
    body.append(item.root);
  }

  function update(index) {
    state.index = index;
    const upTo = horizon();
    const now = state.whole ? null : state.events[index];
    // The record is over here only at the last line of a run that has finished, or when the whole of it is asked for.
    const ended = state.whole || (!state.live && index >= state.events.length - 1);
    bar.set(funnel(state.trail, upTo));
    let showing = 0;
    state.rows.forEach((item) => {
      if (item.begins > upTo) {
        // Nobody has mentioned this ticker by this line (the playback went back): it is not on the page.
        if (item.root) body.removeChild(item.root);
        item.root = null;
        return;
      }
      if (!item.root) open(item);
      showing += 1;
      let first = -1, furthest = -1;
      item.cells.forEach((part, at) => {
        const step = item.row[part.name];
        const done = reached(step, upTo);
        const full = done ? `${item.row.ticker} ${NAMES[part.name]}：${stepText(part.name, step, { upTo })}` : "";
        part.node.hidden = !done;
        if (full !== part.said) {
          const leaning = done ? lean(part.name, step) : "";
          part.glyph.textContent = GLYPH[leaning] ? `${GLYPH[leaning]} ` : "";
          part.text.textContent = done ? stepText(part.name, step, { brief: true, upTo }) : "";
          if (done) {
            part.node.setAttribute("aria-label", full);
            part.node.dataset.lean = leaning;
          } else {
            part.node.removeAttribute("aria-label");
            delete part.node.dataset.lean;
          }
          part.said = full;
        }
        if (!done) {
          delete part.node.dataset.state;
          return;
        }
        part.node.dataset.state = !state.whole && isLine(part.name, step, index) ? "now" : "done";
        if (first < 0) first = at;
        furthest = at;
      });
      // The rail runs from the first step this ticker has taken to the furthest one so far.
      item.lit.style.left = `${((first + 0.5) / STEPS.length) * 100}%`;
      item.lit.style.width = `${((furthest - first) / STEPS.length) * 100}%`;
      item.held.hidden = !(reached(item.row.view, upTo) && item.row.view.held);
      item.again.hidden = !(item.row.repeats && reached(item.row.proposal, upTo));
      item.again.textContent = item.again.hidden ? "" : `另有 ${item.row.repeats} 条重复提案不算`;
      const text = outcomeText(outcome(item.row, upTo), ended);
      if (text !== item.said) {
        item.result.textContent = text;
        if (item.said !== null) replay(item.result, "bump");
        item.said = text;
      }
      // The line being said is about this ticker, or is the manager's list with this ticker on it.
      const active = Boolean(now) && (now.ticker === item.row.ticker || (item.row.proposal !== null && item.row.proposal.index === index));
      item.root.classList.toggle("active", active);
    });
    head.hidden = showing === 0;
    none.hidden = showing > 0;
    none.textContent = state.events.length ? "到这一句为止，还没有哪只股票走到观点这一步。" : "这一场还没有记录。";
    // Old orders that filled or were taken back, once the record has got to them.
    const opened = state.trail.flatMap((row) => row.fills.filter((fill) => fill.index <= upTo).map((fill) =>
      `${row.ticker} ${word("side", fill.side)} ${fill.shares} 股${fill.pnl_pct === null ? "" : `（${signedPct(fill.pnl_pct)}）`}`));
    const taken = state.trail.filter((row) => reached(row.withdrawn, upTo)).map((row) =>
      `${row.ticker} ${word("side", row.withdrawn.side)} ${row.withdrawn.shares} 股`);
    aside.textContent = [
      opened.length ? `这一场开头成交的旧订单：${opened.join("，")}。` : "",
      taken.length ? `这一场撤回的旧订单：${taken.join("，")}。` : "",
    ].join("");
    aside.hidden = !aside.textContent;
  }

  /**
   * Take a session's record. `run` names the run it is the record of, and
   * `live` says that run is still going. When the same run comes again with
   * more lines, the rows on the page stay where they are and read the longer
   * record; anything else starts the trail over.
   */
  function load(events, run, live = false) {
    state.events = events;
    state.live = live;
    if (run === state.run && events.length === state.count) return;
    state.trail = buildTrail(events);
    // In the order the record gets to them. A row that holds only old orders filling has no decision to show.
    const rows = state.trail.map((row) => ({ row, begins: firstLine(row), root: null }))
      .filter((item) => item.begins !== null)
      .sort((a, b) => a.begins - b.begins);
    const grown = run === state.run && state.rows.every((item, at) => rows[at] && rows[at].row.ticker === item.row.ticker);
    if (grown) {
      state.rows.forEach((item, at) => { rows[at] = Object.assign(item, { row: rows[at].row, begins: rows[at].begins }); });
    } else {
      clear(body).append(head);
    }
    state.rows = rows;
    state.run = run;
    state.count = events.length;
  }

  return { node, load, update };
}
