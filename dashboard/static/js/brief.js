// The meeting at a glance, above the room: where it ended (the macro view, how
// many tickers got how far, and each ticker's outcome), and the stages it went
// through with the place of the line being played. The outcome is the whole
// record's, whatever line the playback is at: a reader who wants the result
// does not have to play the meeting to get it. Every word here is the trail's
// own fixed vocabulary or a recorded number; nobody's sentence is rewritten.

import { el, clear, fill, showTip, hideTip } from "./dom.js";
import { stagesOf, summaryOf } from "./links.js";
import { countUp, replay } from "./motion.js";
import { t } from "./i18n.js";

const GLYPH = { up: "▲", down: "▼", stop: "✕", flat: "•" };
// Each step: the count's name in the summary, a word short enough to stand under a figure, and what it counts.
const FLOW = [
  ["views", t("观点"), t("分析师写了观点的股票")], ["ruled", t("裁决"), t("经过辩论、裁判做了裁决的")], ["proposed", t("提案"), t("组合经理提出要买、卖或减仓的")],
  ["accepted", t("通过"), t("提案通过规则检查的")], ["sized", t("放行"), t("风控批准或削减后放行的")], ["ordered", t("下单"), t("最后下了订单的")],
];
// Each group: its name, what it holds, and whether a chip carries the outcome's words or the ticker alone.
const GROUPS = [
  ["acted", t("有动作"), t("下了单、撤了单，或者记下留给收盘会的"), true],
  ["stopped", t("被拦下"), t("提案被规则退回，或者订单被风控拒绝的"), true],
  ["kept", t("保持不动"), t("继续持有、没有改动的"), false],
  ["open", t("只到讨论"), t("有观点、辩论或提案，但没有走到下单的"), false],
];
const ARROW = { up: "▲ ", down: "▼ ", flat: "" };

/**
 * The outcome of the meeting in the room. `load(events, { run, live })`
 * takes the record; `onSeek(line)` is called with the place of the line an
 * outcome was said at.
 */
export function meetingBrief({ onSeek }) {
  const title = el("h2", {});
  const board = el("span", { class: "brief-board", hidden: true });
  const flow = el("ol", { class: "brief-flow", "aria-label": t("有多少只股票走到了哪一步") });
  const groups = el("div", { class: "brief-groups" });
  const node = el("section", { class: "card brief" }, el("div", { class: "brief-head" }, title, board), flow, groups);
  let shown = { key: null, run: null };

  function chip(item, worded) {
    const button = el("button", {
      type: "button", class: "out", "data-lean": item.lean,
      "aria-label": t`${item.ticker}：${item.text}。点一下，跳到说这句话的地方`,
      onclick: () => { if (item.line !== null) onSeek(item.line); },
    }, el("i", { "aria-hidden": "true", text: GLYPH[item.lean] }), el("b", { text: item.ticker }), worded ? el("span", { text: item.text }) : null);
    button.addEventListener("pointermove", (pointer) => showTip(pointer.clientX, pointer.clientY, item.ticker,
      [{ value: item.text, name: "", plain: true }, { value: t("点一下，跳到这句原话"), name: "", plain: true }]));
    button.addEventListener("pointerleave", hideTip);
    return button;
  }

  function load(events, { run = "", live = false } = {}) {
    const key = `${run}:${events.length}:${live}`;
    if (key === shown.key) return;
    const fresh = run !== shown.run;                       // another meeting: its figures run up from nothing
    shown = { key, run };
    title.textContent = live ? t("团队目前的进展") : t("团队给你的结论");
    clear(flow);
    clear(groups);
    hideTip();
    if (events.length === 0) {
      board.hidden = true;
      groups.append(el("p", { class: "muted", text: t("这一场还没有记录。") }));
      return;
    }
    const summary = summaryOf(events, !live);
    board.hidden = !summary.board;
    if (summary.board) {
      const { text, lean, reused } = summary.board;
      fill(board, el("small", { text: t("宏观结论") }),
        el("strong", { class: lean === "flat" ? null : lean, text: `${ARROW[lean] || ""}${text}` }),
        reused ? el("small", { text: t`沿用 ${reused} 的` }) : null);
    }
    const widest = Math.max(1, summary.funnel.views, summary.funnel.proposed);
    FLOW.forEach(([name, label, what]) => {
      const value = summary.funnel[name];
      const figure = el("b", { text: String(value) });
      if (fresh) countUp(figure, value, (now) => String(Math.round(now)), 600);
      flow.append(el("li", { class: value ? "flow-step" : "flow-step none", title: t`${what}：${value} 只` }, figure, el("span", { text: label }),
        el("i", { class: "flow-bar", style: `--share: ${Math.round((value / widest) * 100)}%` })));
    });
    const any = GROUPS.some(([name]) => summary.groups[name].length);
    if (!any) {
      groups.append(el("p", { class: "muted", text: live ? t("到这一句为止，还没有哪只股票走到观点这一步。") : t("这一场没有对任何股票做出决定。") }));
      return;
    }
    for (const [name, label, what, worded] of GROUPS) {
      const items = summary.groups[name];
      if (items.length === 0) continue;
      groups.append(el("div", { class: `brief-group ${name}` },
        el("div", { class: "brief-label", title: what }, el("span", { text: label }), el("b", { text: String(items.length) })),
        el("div", { class: "brief-chips" }, items.map((item) => chip(item, worded)))));
    }
    if (fresh) replay(node, "arrive");
  }

  return { node, load };
}

/**
 * The stages of the meeting as a row of steps, the one being played lit and
 * filled as far as its lines have been said. `update(events, index, run)`
 * moves it; `onSeek(line)` is called with a stage's first line.
 */
export function stageRail({ onSeek }) {
  const node = el("ol", { class: "rail", "aria-label": t("这一场走过的阶段") });
  let built = null;
  const steps = new Map();

  function update(events, index, run = "") {
    const stages = stagesOf(events, index);
    const key = `${run}|${stages.map((stage) => `${stage.stage}:${stage.count}`).join(",")}`;
    if (key !== built) {
      built = key;
      clear(node);
      steps.clear();
      for (const stage of stages) {
        const count = el("small", {});
        const bar = el("i", { class: "rail-fill" });
        const button = el("button", { type: "button", class: "rail-step", onclick: () => onSeek(stage.from) },
          el("span", { text: stage.title }), count, bar);
        steps.set(stage.stage, { button, count, bar, state: null });
        node.append(el("li", {}, button));
      }
    }
    for (const stage of stages) {
      const step = steps.get(stage.stage);
      step.count.textContent = stage.state === "now" || stage.state === "part" ? `${stage.done}/${stage.count}` : String(stage.count);
      step.bar.style.width = `${Math.round((stage.done / stage.count) * 100)}%`;
      step.button.setAttribute("aria-current", stage.state === "now" ? "step" : "false");
      step.button.title = t`${stage.title}：共 ${stage.count} 句，已播 ${stage.done} 句。点一下，跳到这一段的第一句`;
      if (step.state !== stage.state) {
        step.button.dataset.state = stage.state;
        if (stage.state === "now" && step.state !== null) replay(step.button, "lit");
        step.state = stage.state;
      }
    }
  }

  return { node, update };
}
