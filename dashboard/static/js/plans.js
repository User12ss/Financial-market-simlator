// The desk's plans, on the 持仓 page: for each trade, what would show it wrong and what is done then, which of
// those conditions have held and what came of them, and how the plan changed from version to version. The
// conditions are put into words here from their recorded kinds and numbers. Everything a model or the pipeline
// wrote (a thesis, a reason, what was seen, what was done) is shown exactly as recorded, in its own language.
// The rules are plain functions, so they can be tested with no page in sight.

import { el, card, empty, num, rate, state, table, usd } from "./dom.js";
import { nyClock } from "./backdrop.js";
import { t } from "./i18n.js";

// The three ways the simulation says a trade may end, by the record's own names.
export const ENDINGS = { good: t("先到目标"), bad: t("先碰止损"), middle: t("两者都没到") };
const STATES = {
  waiting: ["idle", t("尚未触发")], fired: ["warning", t("已触发，待处理")], done: ["good", t("已执行")],
  not_done: ["critical", t("未执行")], heard: ["info", t("已上会")],
};

/** A condition of a plan in words, from its recorded kind and figures. A kind the page does not know is shown as recorded. */
export function conditionText(condition) {
  const level = condition.level;
  switch (condition.kind) {
    case "price_below": return t`价格跌到 ${num(level, 2)} 或以下`;
    case "price_above": return t`价格涨到 ${num(level, 2)} 或以上`;
    case "close_below_average": return t`收盘跌破 ${condition.average} 日均线`;
    case "date": return t`到 ${condition.on} 收盘`;
    case "chance_below": return t`模拟的『${ENDINGS[condition.ending] || condition.ending}』概率低于 ${rate(level)}`;
    case "chance_above": return t`模拟的『${ENDINGS[condition.ending] || condition.ending}』概率高于 ${rate(level)}`;
    case "headline": return t`新闻标题出现 ${(condition.keywords || []).join(" / ")}`;
    default: return String(condition.kind);
  }
}

/** What a plan says to do when a condition holds. */
export function actionText(item) {
  switch (item.action) {
    case "exit": return t("卖出全部");
    case "trim": return t("卖出一半");
    case "withdraw": return t("撤回未成交的买单");
    case "tighten_stop": return t`止损上调到 ${num(item.new_stop, 2)}`;
    case "call_meeting": return t("召集会议");
    default: return String(item.action);
  }
}

export const whoText = (item) => (item.automatic ? t("代码自动执行") : t("开会决定"));
export const sourceText = (source) => (source === "default" ? t("系统默认") : t("组合经理起草"));

/** Where a condition stands, as a mark and a label: never colour alone. */
export function stateBadge(name) {
  const [level, label] = STATES[name] || ["idle", String(name)];
  return state(level, label);
}
export const stateText = (name) => (STATES[name] || [null, String(name)])[1];

/** When and by whom a condition was found to hold: its trading day and the New York clock. */
export function foundText(trigger) {
  const clock = nyClock(trigger.fired_at, false) || "—";
  if (trigger.found_by === "watch") return t`${trigger.session_date} ${clock}（纽约时间）由盘中巡检发现`;
  if (trigger.found_by === "close") return t`${trigger.session_date} ${clock}（纽约时间）由收盘会发现`;
  return t`${trigger.session_date} ${clock}（纽约时间）发现`;
}

/** Whether a plan's trade is still a buy waiting to fill. The book says so where it can; the plan's version otherwise. */
export const waiting = (plan, latest) => (plan.held_now === null || plan.held_now === undefined ? !latest.held : !plan.held_now && !latest.held);

/** The record beside a condition: its reason, and once it has held, what was seen and what was done. All verbatim. */
function conditionRecord(row) {
  const trigger = row.trigger;
  return el("div", { class: "stack-tight" },
    el("span", { class: "said", text: row.reason }),
    trigger ? el("div", { class: "plan-fired small" },
      el("span", { class: "muted", text: foundText(trigger) }),
      el("span", { class: "said", text: trigger.evidence }),
      trigger.result ? el("span", { class: "said", text: trigger.result }) : null) : null);
}

function conditionName(row) {
  return el("div", { class: "stack-tight" },
    el("span", {}, el("span", { class: "muted", text: `[${row.key}] ` }), conditionText(row.condition)),
    row.source === "rule" ? el("span", { class: "small muted", text: t("每个计划都有的规则") }) : null,
    row.dropped ? el("span", { class: "small muted", text: t`第 ${row.in_version} 版之后已去掉` }) : null);
}

export function conditionsTable(conditions) {
  return table([
    { title: t("条件"), cell: conditionName },
    { title: t("动作"), cell: (row) => actionText(row) },
    { title: t("谁来做"), cell: (row) => whoText(row) },
    { title: t("状态"), cell: (row) => stateBadge(row.state) },
    { title: t("理由和经过（原文）"), said: true, cell: conditionRecord },
  ], conditions, t("这个计划没有条件。"));
}

/** The three endings' chances in one line, with where they come from. Null when the version has none. */
export function chancesLine(version) {
  const chances = version.chances || {};
  if (!Object.keys(chances).length) return null;
  return el("div", { class: "row small plan-chances" },
    Object.keys(ENDINGS).map((ending) => el("span", { class: "chip chip-quiet" }, ENDINGS[ending], " ", el("strong", { text: rate(chances[ending]) }))),
    el("span", { class: "muted", text: version.target === null || version.target === undefined
      ? t`取自 ${version.session_date} 收盘会的模拟。`
      : t`取自 ${version.session_date} 收盘会的模拟；目标 ${usd(version.target)} 是名义目标。` }));
}

const list = (items) => el("ul", { class: "plain said" }, items.map((text) => el("li", { text })));

/** One version of a plan: when and by whom, why it was looked at, what changed and what was refused. */
export function versionEntry(version) {
  return el("li", { class: "plan-version" },
    el("div", { class: "row small" },
      el("strong", { text: t`第 ${version.version} 版` }), el("span", { text: version.session_date }),
      el("span", { class: "chip chip-quiet", text: sourceText(version.source) })),
    version.basis ? el("p", { class: "small said", text: version.basis }) : null,
    version.version === 1 && !(version.changes || []).length
      ? el("p", { class: "small muted", text: t("第一版") })
      : (version.changes || []).length ? list(version.changes) : null,
    (version.refused || []).length ? el("div", {}, el("h3", { text: t("被拒绝的部分（原文）") }), list(version.refused)) : null,
    (version.data_warnings || []).length ? el("div", {}, el("h3", { text: t("数据提醒（原文）") }), list(version.data_warnings)) : null);
}

/** A plan that stands: its header, its thesis, the simulation's chances, its conditions and its versions, newest first. */
export function standingPlan(plan) {
  const latest = plan.versions[plan.versions.length - 1];
  return el("article", { class: "plan" },
    el("div", { class: "row plan-head" },
      el("strong", { class: "plan-ticker", text: plan.ticker }),
      el("span", { class: "chip", text: t`第 ${latest.version} 版` }),
      el("span", { class: "chip chip-quiet", text: sourceText(latest.source) }),
      el("span", { class: "small", text: t`止损 ${usd(latest.stop)}` }),
      el("span", { class: "small", text: t`期限到 ${latest.ends_on} 收盘` }),
      waiting(plan, latest) ? state("warning", t("买单待成交")) : null),
    el("p", { class: "said", text: latest.thesis }),
    chancesLine(latest),
    conditionsTable(plan.conditions),
    el("details", { class: "plan-versions" },
      el("summary", { text: t`版本记录（${plan.versions.length} 版，最新的在最上面）` }),
      el("ul", { class: "plan-timeline" }, plan.versions.slice().reverse().map(versionEntry))));
}

/** The final state of each condition of a plan that is over, in a row of marks. */
const finalStates = (conditions) => el("div", { class: "row plan-finals" },
  conditions.map((row) => el("span", { class: "small", title: conditionText(row.condition) },
    el("span", { class: "muted", text: `[${row.key}] ` }), stateBadge(row.state))));

export function endedPlans(ended) {
  return el("details", { class: "plan-ended" },
    el("summary", { text: t`已结束的计划（${ended.length} 个）` }),
    table([
      { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
      { title: t("期间"), cell: (row) => t`${row.opened} 至 ${row.closed_on}` },
      { title: t("版本数"), num: true, cell: (row) => num(row.versions) },
      { title: t("条件的最终状态"), cell: (row) => finalStates(row.conditions) },
      { title: t("结束原因（原文）"), said: true, cell: (row) => row.closed_why },
    ], ended));
}

/** The whole section. `data` is the server's answer for /api/plans, or null when it could not be read. */
export function plansSection(data) {
  const lead = t("每笔买入和每只持仓在收盘会上定下的计划：什么情况说明看错了，到时候做什么。")
    + t("条件成立时，“代码自动执行”的由代码在收盘会上去做；“开会决定”的只召集会议，做不做由会上决定。");
  if (!data) return card(t("作战计划"), null, empty(t("读不到作战计划。")));
  if (!String(data.arm || "").startsWith("desk/")) return card(t("作战计划"), null, empty(t("这一组不做作战计划。只有会议室一组有。")));
  if (!data.has_plans) return card(t("作战计划"), lead, empty(t("还没有计划。会议室在收盘会上为每笔买入和每只持仓生成计划。")));
  return card(t("作战计划"), lead,
    data.standing.length ? data.standing.map(standingPlan) : empty(t("现在没有生效的计划。")),
    data.ended.length ? endedPlans(data.ended) : null);
}
