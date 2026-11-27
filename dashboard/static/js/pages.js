// The pages. Each one reads from the server and lays out what it got. None
// has a control that changes anything: there is nothing here to press that
// places an order, moves a limit or starts a run.

import {
  el, clear, getJSON, usd, signedUsd, pct, signedPct, rate, num, when, span, bytes, word, delta, state, runState,
  tile, card, empty, table, hideTip, DASH,
} from "./dom.js";
import { lineChart, rateBars, quantileStrip, meter } from "./charts.js";
import { dayStrip } from "./day.js";
import { buildTrail, fateText, funnel, outcome, outcomeText, planText } from "./decisions.js";
import { roomFeed } from "./live.js";
import { meetingRoom } from "./meeting.js";
import { countUp } from "./motion.js";
import { plansSection } from "./plans.js";
import { decisionTrail, funnelBar } from "./trail.js";
import { t } from "./i18n.js";

const SERIES = { account: "var(--series-1)", benchmark: "var(--series-2)", candidates: "var(--series-3)" };
const money = (value, short) => usd(value, short ? 0 : 2);

/** A figure that runs up to its value when it first appears. It always ends on exactly that value. */
function counted(value, write) {
  const node = el("span", {});
  countUp(node, value, write);
  return node;
}

/**
 * A page that shows one answer from the server and repaints only when the answer has changed.
 * `source` is the address to read, or a function that reads and returns the answer.
 */
function dataPage(ctx, source, paint) {
  const read = typeof source === "function" ? source : () => getJSON(source, { arm: ctx.arm });
  const warning = el("p", { class: "banner", hidden: true });
  const body = el("div", { class: "stack" });
  const node = el("div", { class: "stack" }, warning, body);
  let last = null;                                   // what is on the page now, when it was read successfully
  async function refresh() {
    const answer = await read();
    if (answer.status === 0 && last !== null) {
      // The server cannot be reached. What was last read stays up, marked as such, rather than vanishing.
      warning.textContent = t`⚠ ${answer.data.error}下面是最后一次读到的内容，可能已经过时。`;
      warning.hidden = false;
      return;
    }
    warning.hidden = true;
    const text = JSON.stringify(answer.data);
    if (answer.ok && text === last) return;
    last = answer.ok ? text : null;
    clear(body);
    if (answer.ok) paint(body, answer.data);
    else body.append(empty(answer.data.error || t("读不到数据。")));
  }
  refresh();
  return { node, refresh };
}

// ── 总览 ─────────────────────────────────────────────────────────────────────

function guards(data) {
  const { kill_switch: halt, daily_loss: day, drawdown: fall } = data.guards;
  const line = (name, tripped, bar) => el("div", { class: "stack-tight" },
    el("div", { class: "row spread" },
      el("strong", { text: name }), state(tripped ? "critical" : "good", tripped ? t("已触发：不开新仓") : t("未触发"))),
    bar);
  return card(t("熔断和停机开关"), t("这些由代码判断，模型改不了，这个页面也改不了。"),
    el("div", { class: "stack" },
      el("div", { class: "stack-tight" },
        el("div", { class: "row spread" },
          el("strong", { text: t("停机开关") }),
          state(halt.on ? "critical" : "good", halt.on ? t("已启用：不下任何新订单") : t("没有启用"))),
        halt.on ? el("p", { class: "small said", text: t`原因：${halt.reason}` }) : null,
        el("p", { class: "small muted" },
          t("这是看板此刻看到的状态。开关是这个文件，存在即停机："), el("code", { text: halt.file }),
          t("。只给流程单独设置的环境变量 TRADING_HALT，看板看不到。")),
        // What the pipeline itself wrote down the last time it finished: this part is the record.
        halt.last_run ? el("p", { class: "small" },
          t`最近完成的一场（${halt.last_run.session}）自己记下的：`,
          halt.last_run.halted ? el("span", { class: "said", text: halt.last_run.halted }) : t("当时没有停机。")) : null),
      line(t("当日亏损熔断"), day.tripped, meter({
        name: t("当日净值变化"), valueText: day.value_pct === null ? t("还没有数据") : signedPct(day.value_pct),
        limitText: `−${day.limit_pct}%`, share: day.value_pct !== null && day.value_pct < 0 ? -day.value_pct / day.limit_pct : 0,
        tripped: day.tripped,
      })),
      line(t("最大回撤熔断"), fall.tripped, meter({
        name: t("距最高点的回撤"), valueText: fall.value_pct === null ? t("还没有数据") : pct(fall.value_pct),
        limitText: `${fall.limit_pct}%`, share: fall.value_pct === null ? 0 : fall.value_pct / fall.limit_pct,
        tripped: fall.tripped,
      }))));
}

function paintOverview(node, data) {
  const account = data.account;
  node.append(el("div", { class: "row spread" },
    el("h1", { text: t("总览") }),
    el("div", { class: "row" },
      el("span", { class: "small muted", text: data.latest_run ? t`最近一次运行：${data.latest_run.session}` : "" }),
      runState(data.latest_run))));
  if (!account) {
    node.append(empty(t("这套模型配置还没有模拟账户：流程还没有走到下单那一步。")));
    return;
  }
  node.append(el("div", { class: "kpis" },
    tile(t("账户净值"), counted(account.equity, usd), t`截至 ${account.as_of || DASH} 收盘`, true),
    tile(t("累计收益"), delta(account.return_pct, counted(account.return_pct, signedPct)), t`初始资金 ${usd(account.initial_cash, 0)}`),
    tile(t("当日盈亏"), delta(account.day_change_pct, counted(account.day_change_pct, signedPct)),
      account.day_change_pct === null ? t("只有一个交易日，还算不出") : t("相对上一个交易日")),
    tile(t("现金"), counted(account.cash, usd), account.equity ? t`占净值 ${pct((account.cash / account.equity) * 100, 1)}` : null),
    tile(t("持仓市值"), counted(account.positions_value, usd), t`${data.positions} 只持仓，${data.open_orders} 张订单等待成交`),
    tile(t("距最高点回撤"), counted(account.drawdown_pct, pct), t`最高点 ${usd(account.high_water)}`)));

  const series = data.curves.map((curve) => ({ name: curve.name, color: SERIES[curve.key], points: curve.points }));
  const about = data.evaluated_as_of
    ? t`两条基准线取自 ${data.evaluated_as_of} 的评估报告。第三个基准（随机挑选）是一个分布，不是一条线，在“评估”页。`
    : t("还没有评估报告，所以只有账户自己这一条线。评估跑过一次之后，这里会多出两条基准线。");
  node.append(el("div", { class: "grid cols-2" },
    card(t("净值和基准"), about, lineChart({ series, valueText: money, label: t("账户净值和基准的走势") })),
    guards(data)));
  if (data.meeting && data.meeting.run) node.append(lastDecisions(data.meeting));
}

/** The latest session in one row: how many views became how many orders, and which ones. */
function lastDecisions(meeting) {
  const trail = buildTrail(meeting.events);
  const bar = funnelBar();
  bar.set(funnel(trail));
  const placed = trail.filter((row) => outcome(row).kind === "ordered");
  // The address starts with "#replay/" and ends in a date the server sent, so it can only lead to this page's own replay.
  return card(t`${meeting.session} 收盘会的决策`, t("从分析师的观点到最后下的订单，每一步剩下多少只。"),
    bar.node,
    el("p", { class: "small" }, placed.length ? t("下了订单的：") : t("这一场没有下订单。"),
      placed.map((row) => el("span", { class: "chip", text: `${row.ticker} ${outcomeText(outcome(row), true).replace(t("已下单："), "")}` }))),
    el("p", { class: "small" }, el("a", { href: `#replay/${meeting.session}`, text: t("去回放看这一场是怎么讨论出来的 →") })));
}

/**
 * The first page needs the account and, for its last card, the record of the latest close meeting: the meeting
 * that decides what is bought. (A desk's check-ins in between can only cut or take back, and have the room.)
 */
async function readOverview(ctx) {
  const [overview, meeting] = await Promise.all([
    getJSON("/api/overview", { arm: ctx.arm }), getJSON("/api/meeting", { arm: ctx.arm, kind: "close" })]);
  if (!overview.ok) return overview;
  return { ...overview, data: { ...overview.data, meeting: meeting.ok ? meeting.data : null } };
}

export const overviewPage = (ctx) => dataPage(ctx, () => readOverview(ctx), paintOverview);

// ── 持仓和订单 ───────────────────────────────────────────────────────────────

function paintBook(node, data) {
  node.append(el("h1", { text: t("持仓和订单") }));
  node.append(card(t("持仓"), t("市值按最近一个收盘价算。"), table([
    { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
    { title: t("行业"), cell: (row) => row.sector },
    { title: t("股数"), num: true, cell: (row) => num(row.shares) },
    { title: t("成本价"), num: true, cell: (row) => usd(row.avg_cost) },
    { title: t("最近收盘"), num: true, cell: (row) => usd(row.last_close) },
    { title: t("市值"), num: true, cell: (row) => usd(row.value) },
    { title: t("浮动盈亏"), num: true, cell: (row) => delta(row.unrealized_usd, `${signedUsd(row.unrealized_usd)}（${signedPct(row.unrealized_pct)}）`) },
    { title: t("止损价"), num: true, cell: (row) => usd(row.stop_price) },
    { title: t("买入日"), cell: (row) => row.entry_date },
    { title: t("持有交易日"), num: true, cell: (row) => num(row.sessions_held) },
  ], data.positions, t("现在没有持仓。"))));

  node.append(card(t("挂着的订单"), t("还没有成交的订单，包括每个持仓的保护性止损。"), table([
    { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
    { title: t("方向"), cell: (row) => word("side", row.side) },
    { title: t("类型"), cell: (row) => word("kind", row.kind) },
    { title: t("股数"), num: true, cell: (row) => num(row.shares) },
    { title: t("限价"), num: true, cell: (row) => usd(row.limit_price) },
    { title: t("止损价"), num: true, cell: (row) => usd(row.stop_price ?? row.protective_stop) },
    { title: t("下单的交易日"), cell: (row) => row.created_session },
    { title: t("下单理由（原文）"), said: true, cell: (row) => row.reason },
  ], data.orders, t("现在没有挂着的订单。"))));

  node.append(card(t("最近的成交"), t("最新的在最上面。"), table([
    { title: t("成交日"), cell: (row) => row.fill_date },
    { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
    { title: t("方向"), cell: (row) => word("side", row.side) },
    { title: t("类型"), cell: (row) => word("kind", row.kind) },
    { title: t("股数"), num: true, cell: (row) => num(row.shares) },
    { title: t("成交价"), num: true, cell: (row) => usd(row.price, 4) },
    { title: t("相对成本"), num: true, cell: (row) => (row.pnl_pct === null ? null : delta(row.pnl_pct, signedPct(row.pnl_pct))) },
    { title: t("备注（原文）"), said: true, cell: (row) => row.note },
  ], data.fills, t("还没有成交。"))));

  const signals = data.signals;
  if (!signals) {
    node.append(card(t("最近一场的候选和信号"), null, empty(t("还没有完成过一场。"))));
    return;
  }
  const regime = signals.regime;
  node.append(card(t`${signals.session} 这一场的候选和信号`, t("最近一场完成的流程留下的结果。"),
    el("div", { class: "kpis" },
      tile(t("这一场"), word("run", signals.status), signals.halted ? t("当时处于停机状态") : null),
      tile(t("股票池"), t`${num(signals.universe_size)} 只`, t`筛出 ${signals.candidates.length} 只候选`),
      tile(t("宏观风险偏好"), regime ? word("appetite", regime.risk_appetite) : DASH,
        regime ? t`把握度 ${num(regime.confidence, 2)}，形成于 ${regime.formed_on}` : t("这一场没有宏观结论"))),
    el("p", { class: "small" }, t("候选："), signals.candidates.length
      ? signals.candidates.map((ticker) => el("span", { class: "chip", text: ticker }))
      : el("span", { class: "muted", text: t("没有") })),
    signals.warnings.length ? el("div", {},
      el("h3", { text: t("这一场记下的提醒（原文）") }),
      el("ul", { class: "plain said" }, signals.warnings.map((text) => el("li", { text })))) : null));

  node.append(card(t("分析师的观点"), t("每只候选和持仓各一条。"), table([
    { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
    { title: t("已持有"), cell: (row) => (row.held ? t("是") : t("否")) },
    { title: t("方向"), cell: (row) => word("direction", row.direction) },
    { title: t("把握度"), num: true, cell: (row) => num(row.confidence, 2) },
    { title: t("期限（交易日）"), num: true, cell: (row) => num(row.horizon_days) },
    { title: t("建议止损"), num: true, cell: (row) => usd(row.proposed_stop) },
    { title: t("论点（原文）"), said: true, cell: (row) => row.thesis },
  ], signals.views, t("这一场没有观点。"))));

  node.append(card(t("组合经理的提案，和规则检查的结果"), null, table([
    { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
    { title: t("动作"), cell: (row) => (row.source === "plan" ? planText(row.action) : word("action", row.action)) },
    { title: t("把握度"), num: true, cell: (row) => num(row.confidence, 2) },
    { title: t("检查结果"), cell: (row) => word("check", row.status) },
    { title: t("理由（原文）"), said: true, cell: (row) => row.reason },
    { title: t("检查备注（原文）"), said: true, cell: (row) => row.note },
  ], signals.proposals, t("这一场没有提案。"))));

  node.append(card(t("风控对每张订单的决定"), null, table([
    { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
    { title: t("方向"), cell: (row) => word("side", row.side) },
    { title: t("申请股数"), num: true, cell: (row) => num(row.requested_shares) },
    { title: t("批准股数"), num: true, cell: (row) => num(row.approved_shares) },
    { title: t("结论"), cell: (row) => word("verdict", row.verdict) },
    { title: t("原因（原文）"), said: true, cell: (row) => (row.reasons || []).join(" ") },
  ], signals.risk, t("这一场没有订单送到风控。"))));
}

/** The page reads the book and, beside it, the desk's plans for the same trades. Both are read on every refresh. */
async function readBook(ctx) {
  const [book, plans] = await Promise.all([getJSON("/api/book", { arm: ctx.arm }), getJSON("/api/plans", { arm: ctx.arm })]);
  if (!book.ok) return book;
  return { ...book, data: { ...book.data, plans: plans.ok ? plans.data : null } };
}

export const bookPage = (ctx) => dataPage(ctx, () => readBook(ctx), (node, data) => {
  paintBook(node, data);
  node.append(plansSection(data.plans));
});

// ── 评估 ─────────────────────────────────────────────────────────────────────

const tallyColumns = (first, group = null) => [
  { title: first, cell: (row) => (group ? word(group, row.label) : row.label) },
  { title: t("有方向的观点"), num: true, cell: (row) => num(row.views) },
  { title: t("方向对了"), num: true, cell: (row) => `${num(row.right)}（${rate(row.hit_rate)}）` },
  { title: t("跑赢基准"), num: true, cell: (row) => `${num(row.beat_benchmark)}（${rate(row.beat_rate)}）` },
  { title: t("平均超额"), num: true, cell: (row) => signedPct(row.mean_edge_pct) },
  { title: t("平均自报把握度"), num: true, cell: (row) => num(row.mean_confidence, 2) },
  { title: t("误差范围"), num: true, cell: (row) => (row.margin === null ? null : t`±${Math.round(row.margin * 100)} 个点`) },
];

const sliceColumns = (first) => [
  { title: first, cell: (row) => row.label },
  { title: t("笔数"), num: true, cell: (row) => num(row.trades) },
  { title: t("盈亏"), num: true, cell: (row) => delta(row.pnl_usd, signedUsd(row.pnl_usd)) },
];

function paintScorecard(node, report, benchmark) {
  const score = report.scorecard;
  const overall = score.overall;
  node.append(card(t("观点的成绩"), t`每条观点从下一个交易日开盘算到期限结束的收盘，和 ${benchmark} 同期比，含分红。`,
    el("div", { class: "kpis" },
      tile(t("观点总数"), num(score.total), t`${num(score.pending)} 条还没到期，${num(score.unpriced)} 条算不出价格`),
      tile(t("已经评分"), num(score.scored), t`来自 ${num(score.sessions)} 个交易日，其中中性 ${num(score.neutral)} 条`),
      tile(t("方向命中率"), overall ? rate(overall.hit_rate) : DASH,
        overall && overall.margin !== null ? t`误差范围约 ±${Math.round(overall.margin * 100)} 个点` : t("还没有可评分的观点")),
      tile(t("跑赢基准的比例"), overall ? rate(overall.beat_rate) : DASH,
        overall ? t`平均超额 ${signedPct(overall.mean_edge_pct)}` : null)),
    score.scored === 0 ? empty(t("观点都还没到期，现在没有成绩可看。到期两个交易日之后才会评分。")) : null));
  if (score.scored === 0) return;

  node.append(card(t("把握度准不准"), t("模型说自己有多大把握，和实际对了多少。条是实际命中率，竖线是这一档里模型自报的平均把握度。"),
    score.by_confidence.length
      ? el("div", {},
        rateBars({
          rows: score.by_confidence.map((row) => ({
            label: t`把握度 ${row.label.replace(" to ", "–")}`, value: row.hit_rate, marker: row.mean_confidence,
            note: t`${row.views} 条观点`,
          })),
          barName: t("实际命中率"), markerName: t("自报把握度（平均）"), label: t("各档把握度的实际命中率"),
        }),
        el("details", {}, el("summary", { text: t("查看数据表") }), table(tallyColumns(t("把握度")), score.by_confidence)))
      : empty(t("还没有按把握度分档的成绩。"))));
  node.append(el("div", { class: "grid cols-2" },
    card(t("按方向"), null, table(tallyColumns(t("方向"), "direction"), score.by_direction)),
    card(t("按是否已持有"), null, table(tallyColumns(t("类别"), "position"), score.by_position)),
    card(t("按行业"), null, table(tallyColumns(t("行业")), score.by_sector)),
    card(t("按当时的宏观环境"), null, table(tallyColumns(t("风险偏好"), "appetite"), score.by_regime))));
}

function paintTrades(node, report) {
  const trades = report.trades;
  node.append(card(t("交易的结果"), t("一笔交易从买入算到全部卖出。"),
    el("div", { class: "kpis" },
      tile(t("已平仓"), t`${num(trades.closed)} 笔`, t`赢 ${num(trades.winners)}，亏 ${num(trades.losers)}，止损出场 ${num(trades.stopped_out)}`),
      tile(t("胜率"), rate(trades.win_rate), trades.closed < 20 ? t("笔数太少，只能当参考") : null),
      tile(t("已实现盈亏"), delta(trades.realized_usd, signedUsd(trades.realized_usd)),
        t`平均赢 ${usd(trades.average_win_usd)}，平均亏 ${usd(trades.average_loss_usd)}`),
      tile(t("还开着的仓位"), t`${num(trades.open)} 笔`, t`浮动盈亏 ${signedUsd(trades.unrealized_usd)}`)),
    table([
      { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
      { title: t("行业"), cell: (row) => row.sector },
      { title: t("决定日"), cell: (row) => row.decided_on },
      { title: t("买入日"), cell: (row) => row.entry_date },
      { title: t("买入价"), num: true, cell: (row) => usd(row.entry_price, 4) },
      { title: t("股数"), num: true, cell: (row) => num(row.shares) },
      { title: t("状态"), cell: (row) => (row.stopped ? t("止损出场") : word("trade", row.status)) },
      { title: t("已实现"), num: true, cell: (row) => delta(row.realized_usd, signedUsd(row.realized_usd)) },
      { title: t("浮动"), num: true, cell: (row) => (row.unrealized_usd === null ? null : delta(row.unrealized_usd, signedUsd(row.unrealized_usd))) },
      { title: t("持有交易日"), num: true, cell: (row) => num(row.sessions_held) },
    ], trades.trades, t("还没有交易。"))));
  if (trades.trades.length) {
    node.append(el("div", { class: "grid cols-2" },
      card(t("按标的"), null, table(sliceColumns(t("标的")), trades.by_ticker)),
      card(t("按行业"), null, table(sliceColumns(t("行业")), trades.by_sector)),
      card(t("按出场方式"), null, table(sliceColumns(t("出场方式（原文）")), trades.by_exit))));
  }
}

function paintRandom(node, report) {
  const random = report.random;
  if (!random.entries) {
    node.append(card(t("和随机挑选比"), null, empty(t("账户还没有买入过，没有可比的。"))));
    return;
  }
  node.append(card(t("和随机挑选比"),
    t`账户的 ${random.entries} 笔买入，每一笔都换成当天候选里随机的一只，用同一套规则重放 ${num(random.simulations)} 次，估值到 ${random.valued_on}。`,
    el("div", { class: "kpis" },
      tile(t("账户好于多少随机结果"), pct(random.percentile, 1), random.entries < 30 ? t("买入笔数太少，这个位置多半是运气") : null),
      tile(t("账户（按同一规则重放）"), signedUsd(random.replayed_pnl_usd), t`账本上记的是 ${signedUsd(random.booked_pnl_usd)}`),
      tile(t("随机挑选的平均"), signedUsd(random.mean_pnl_usd), t`中位数 ${signedUsd(random.quantiles_usd["50"])}`)),
    quantileStrip({
      quantiles: random.quantiles_usd, value: random.replayed_pnl_usd, valueName: t("这个账户"),
      valueText: (value, short) => signedUsd(value, short ? 0 : 2), label: t("账户的盈亏在随机挑选结果里的位置"),
    }),
    el("h3", { text: t("每一笔买入，在当天候选里排第几") }),
    table([
      { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
      { title: t("决定日"), cell: (row) => row.decided_on },
      { title: t("买入日"), cell: (row) => row.entry_date },
      { title: t("重放盈亏"), num: true, cell: (row) => delta(row.replayed_usd, signedUsd(row.replayed_usd)) },
      { title: t("账本盈亏"), num: true, cell: (row) => signedUsd(row.booked_usd) },
      { title: t("当天候选的平均"), num: true, cell: (row) => signedUsd(row.pool_mean_usd) },
      { title: t("排名"), num: true, cell: (row) => t`第 ${row.rank} / ${row.pool}` },
    ], random.picks)));
}

// What the forecast engine has on record. It is code, read from the journal: the evaluation report is not needed for it.
const STATE = { calm: t("平稳"), turbulent: t("动荡"), crisis: t("危机"), single: t("只有一种状态") };
const WHOSE = { position: t("账上的止损"), analyst: t("分析师提的"), rule: t("仓位规则算的") };
const ahead = (label) => label.replace(/^(\d+) sessions?$/, t("$1 个交易日后"));
// A bar's label has little room: "5 日" here is five sessions ahead, as the line above the bars says.
const brief = (label) => label.replace(/^(\d+) sessions?$/, t("$1 日"));
const threeRates = (side) => ["50", "80", "95"].map((band) => rate(side.coverage[band])).join(" / ");
const pair = (first, second, digits) => `${num(first, digits)} / ${num(second, digits)}`;

function coverageRows(tallies, side) {
  return tallies.flatMap((tally) => ["50", "80", "95"].map((band) => ({
    label: t`${brief(tally.label)} · 中间 ${band}%`, value: tally[side].coverage[band], marker: Number(band) / 100,
    note: t`${tally.scored} 条预测`,
  })));
}

function paintForecasts(node, forecasts) {
  if (!forecasts || !forecasts.sessions || !forecasts.card) return;
  const record = forecasts.card, latest = forecasts.latest;
  node.append(card(t("预测对账"),
    t("量化工程师是代码，不是模型。它在收盘会上给出每只股票 1、5、10、20 个交易日后的价格分布，到期后和实际结果对账。")
    + t("“朴素预测”只用历史波动率，引擎要比它好才算有用。这些预测现在不影响任何订单。"),
    el("div", { class: "kpis" },
      tile(t("留档的预测"), num(record.total), t`来自 ${num(forecasts.sessions)} 场收盘会`),
      tile(t("已经对账"), num(record.scored), t`${num(record.pending)} 条还没到期，${num(record.unpriced)} 条缺价格`)),
    record.scored === 0 ? empty(t("预测都还没到期。最短的期限是 1 个交易日，下一场收盘会就开始对账。")) : el("div", {},
      el("p", { class: "small muted", text:
        t("预测靠得住的话，实际结果落在“中间 50%”里的次数应当接近一半，落在“中间 95%”里的应当接近 95%。条是实际比例，竖线是应有的比例。")
        + t("“5 日 · 中间 80%”指 5 个交易日后的那份预测的中间 80%。")
        + t("同一天做出的预测一起涨跌，所以预测日的个数比预测的条数更能说明证据有多少。") }),
      el("div", { class: "grid cols-2" },
        el("div", {}, el("h3", { text: t("引擎") }), rateBars({
          rows: coverageRows(record.by_horizon, "engine"), barName: t("实际落在区间内"), markerName: t("应有的比例"),
          label: t("引擎的预测：各期限、各区间实际覆盖了多少结果"),
        })),
        el("div", {}, el("h3", { text: t("朴素预测") }), rateBars({
          rows: coverageRows(record.by_horizon, "naive"), barName: t("实际落在区间内"), markerName: t("应有的比例"),
          label: t("朴素预测：各期限、各区间实际覆盖了多少结果"),
        }))),
      el("details", {}, el("summary", { text: t("查看数据表") }), table([
        { title: t("期限"), cell: (row) => ahead(row.label) },
        { title: t("已对账"), num: true, cell: (row) => num(row.scored) },
        { title: t("预测日"), num: true, cell: (row) => num(row.sessions) },
        { title: t("引擎：落在中间 50 / 80 / 95%"), num: true, cell: (row) => threeRates(row.engine) },
        { title: t("朴素：落在中间 50 / 80 / 95%"), num: true, cell: (row) => threeRates(row.naive) },
        { title: t("分位损失，引擎 / 朴素（越低越好）"), num: true, cell: (row) => pair(row.engine.loss, row.naive.loss, 5) },
        { title: t("止损被触及"), num: true, cell: (row) => `${num(row.stops_touched)} / ${num(row.stops)}` },
        { title: t("止损概率的 Brier 分，引擎 / 朴素（越低越好）"), num: true, cell: (row) => pair(row.engine.brier, row.naive.brier, 3) },
      ], record.by_horizon)))));

  if (!latest) return;
  const ten = (item) => item.horizons.find((horizon) => horizon.sessions === 10) || item.horizons[item.horizons.length - 1];
  const book = latest.book && latest.book.positions ? latest.book.horizons.find((horizon) => horizon.sessions === 10) : null;
  node.append(card(t("最近一次预测"),
    t`${latest.session_date} 的收盘会 · 参数第 ${latest.params_version} 版（数据截到 ${latest.params_cutoff}）· `
    + t`${num(latest.paths)} 条模拟路径 · 随机种子 ${latest.seed}。凭这几样可以把这次预测一位不差地重算出来。`,
    el("div", { class: "kpis" }, latest.states.map((name, at) => tile(
      t`市场状态：${STATE[name] || name}`, rate(latest.state_now[at]),
      latest.state_ahead["10"] ? t`10 个交易日后 ${rate(latest.state_ahead["10"][at])}` : null))),
    table([
      { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
      { title: t("收盘"), num: true, cell: (row) => usd(row.close) },
      { title: t("10 个交易日后，一半的路径"), num: true, cell: (row) => `${usd(ten(row).quantiles["25"])} – ${usd(ten(row).quantiles["75"])}` },
      { title: t("九成的路径"), num: true, cell: (row) => `${usd(ten(row).quantiles["5"])} – ${usd(ten(row).quantiles["95"])}` },
      { title: t("止损"), num: true, cell: (row) => (row.stop === null ? null : `${usd(row.stop)}（${WHOSE[row.stop_from] || row.stop_from}）`) },
      { title: t("途中触及止损"), num: true, cell: (row) => rate(ten(row).stop_touch) },
      { title: t("先到名义目标"), num: true, cell: (row) => rate(ten(row).target_first) },
      { title: t("观点给的倾斜和依据（原文）"), said: true, cell: (row) => row.lean_basis || null },
    ], latest.tickers, t("这一场没有能预测的股票。")),
    book ? el("p", { class: "small", text:
      t`组合照现在的 ${num(latest.book.positions)} 只持仓、之后不再交易来算：10 个交易日后净值九成落在 ${usd(book.equity["5"], 0)} 到 `
      + t`${usd(book.equity["95"], 0)}。同一天有两只以上触及止损的可能 ${rate(book.stops_together)}，触及回撤熔断 `
      + t`${rate(book.drawdown_breaker, 1)}，触及单日亏损熔断 ${rate(book.daily_loss_breaker, 1)}。` }) : null,
    latest.problems.length ? el("div", {},
      el("h3", { text: t("没能预测的（原文）") }),
      el("ul", { class: "plain said" }, latest.problems.map((problem) => el("li", { text: problem })))) : null));
}

function paintEvaluation(node, data, ctx) {
  node.append(el("h1", { text: t("评估") }));
  if (!data.as_of) {
    node.append(card(t("还没有评估报告"), null,
      empty(t("评估是单独运行的，这个页面只读它上一次写下的报告，不会自己去跑。"), el("br"), el("code", { text: "python -m evaluation" }))));
  } else {
    node.append(el("p", { class: "small muted" },
      t`报告评估到 ${data.as_of}，写于 ${when(data.created_at)}。这个页面只读这份报告，不会自己重新评估。文件：`,
      el("code", { text: data.file })));
    node.append(card(t("各套模型配置对比"), t("同一段时间，每套配置各自的账户。"), table([
      { title: t("模型配置"), cell: (row) => (row.arm === ctx.arm ? el("strong", { text: row.arm }) : row.arm) },
      { title: t("交易日"), num: true, cell: (row) => num(row.sessions) },
      { title: t("收益"), num: true, cell: (row) => delta(row.return_pct, signedPct(row.return_pct)) },
      { title: t("已评分观点"), num: true, cell: (row) => num(row.scored) },
      { title: t("方向命中率"), num: true, cell: (row) => rate(row.hit_rate) },
      { title: t("跑赢基准"), num: true, cell: (row) => rate(row.beat_rate) },
      { title: t("已平仓"), num: true, cell: (row) => num(row.closed) },
      { title: t("好于多少随机结果"), num: true, cell: (row) => (row.buys ? pct(row.percentile, 1) : null) },
      { title: t("模型调用"), num: true, cell: (row) => num(row.calls) },
      { title: t("费用"), num: true, cell: (row) => (row.cost_usd === null ? t("本地模型，不计费") : usd(row.cost_usd, 4)) },
    ], data.arms)));
  }

  const report = data.report;
  if (report) {
    node.append(card(t("账户和两条基准"), t`${report.first_session || DASH} 到 ${report.last_session || DASH}，共 ${report.sessions_run} 个交易日。`,
      table([
        { title: t("曲线"), cell: (row) => row.title },
        { title: t("收益"), num: true, cell: (row) => delta(row.return_pct, signedPct(row.return_pct)) },
        { title: t("最大回撤"), num: true, cell: (row) => pct(row.max_drawdown_pct) },
        { title: t("报告里的说明（原文）"), said: true, cell: (row) => row.description },
      ], report.curves.map((curve, index) => ({
        ...curve, title: [t("模拟账户"), t`${data.benchmark} 买入持有`, t("候选池等权")][index] || curve.name,
      }))),
      report.notes.length ? el("div", {},
        el("h3", { text: t("报告自己写的提醒（原文）") }),
        el("ul", { class: "plain said" }, report.notes.map((text) => el("li", { text })))) : null));
    paintScorecard(node, report, data.benchmark);
    paintTrades(node, report);
    paintRandom(node, report);
    const usage = report.usage;
    node.append(card(t("模型用量"), t("这套配置到评估日为止的累计。"), el("div", { class: "kpis" },
      tile(t("调用次数"), num(usage.calls)),
      tile(t("输入 / 输出 token"), `${num(usage.input_tokens)} / ${num(usage.output_tokens)}`),
      tile(t("模型用时"), span(usage.model_seconds)),
      tile(t("费用"), usage.cost_usd === null ? t("不计费") : usd(usage.cost_usd, 4), usage.cost_usd === null ? t("本地模型") : null))));
  } else if (data.as_of) {
    node.append(empty(t("这份报告里没有这套模型配置。")));
  }

  paintForecasts(node, data.forecasts);

  node.append(card(t("复盘写下的教训"),
    data.lessons_shown ? t("现在的设置：分析师写观点时会看到这些教训。") : t("现在的设置：这些教训只是存着，没有给分析师看。"),
    table([
      { title: t("标的"), cell: (row) => el("strong", { text: row.ticker }) },
      { title: t("观点日"), cell: (row) => row.session },
      { title: t("可用于"), cell: (row) => t`${row.known_on} 之后` },
      { title: t("通过检查"), cell: (row) => (row.clean ? t("是") : t("否")) },
      { title: t("结果（原文）"), said: true, cell: (row) => row.outcome },
      { title: t("评价（原文）"), said: true, cell: (row) => row.assessment },
      { title: t("教训（原文）"), said: true, cell: (row) => row.lesson },
    ], data.lessons, t("还没有写下任何教训。"))));
}

export const evaluationPage = (ctx) => dataPage(ctx, "/api/evaluation", (node, data) => paintEvaluation(node, data, ctx));

// ── 运行状态 ─────────────────────────────────────────────────────────────────

function paintStatus(node, data) {
  const totals = data.totals;
  node.append(el("h1", { text: t("运行状态") }));
  node.append(el("div", { class: "kpis" },
    tile(t("运行次数"), num(totals.runs), t`失败 ${num(totals.failed || 0)}，降级 ${num(totals.degraded || 0)}`),
    tile(t("模型调用"), num(totals.calls || 0)),
    tile(t("输入 / 输出 token"), `${num(totals.tokens_in || 0)} / ${num(totals.tokens_out || 0)}`),
    tile(t("模型用时"), span(totals.seconds || 0)),
    tile(t("费用"), totals.cost === null ? t("不计费") : usd(totals.cost, 4), totals.cost === null ? t("本地模型") : null)));
  node.append(card(t("每一次运行"),
    t("最新的在最上面。同一个交易日重跑时，之前那次标为“已被重跑取代”。“中断”是看板的判断：账本里还写着运行中，但那个进程已经不在了。"), table([
    { title: t("交易日"), cell: (row) => row.session },
    { title: t("状态"), cell: (row) => runState(row) },
    { title: t("开始"), cell: (row) => when(row.started_at) },
    { title: t("用时"), num: true, cell: (row) => span(row.seconds) },
    { title: t("模型调用"), num: true, cell: (row) => num(row.llm_calls) },
    { title: t("输入 / 输出 token"), num: true, cell: (row) => `${num(row.input_tokens)} / ${num(row.output_tokens)}` },
    { title: t("费用"), num: true, cell: (row) => (row.cost_usd === null ? DASH : usd(row.cost_usd, 4)) },
    { title: t("数据快照"), cell: (row) => (row.snapshot ? (row.snapshot.exists ? t("在") : t("文件不见了")) : t("没有")) },
    // The address starts with "#replay/", so whatever a run is called it can only ever lead to this page's own replay.
    { title: t("发言"), cell: (row) => el("a", { href: `#replay/${row.session}/${encodeURIComponent(row.run_id)}`, text: t("回放这一次") }) },
    { title: t("提醒和错误（原文）"), said: true, cell: (row) => [row.error, ...row.warnings].filter(Boolean).join("\n") || null },
  ], data.runs, t("还没有运行记录。"))));
  node.append(card(t("账本"), t("看板以只读方式打开这个文件，不会写它。"),
    el("p", {}, el("code", { text: data.journal.path }), `　${bytes(data.journal.bytes)}`)));
}

export const statusPage = (ctx) => dataPage(ctx, "/api/status", paintStatus);

// ── 会议室 ───────────────────────────────────────────────────────────────────

/**
 * A room and what keeps it in step with the server (the rules are in live.js), with a place to say what went
 * wrong, and above it the day's strip: the meetings held that day and the watch's last round. `pick(day, id)`
 * is what choosing a meeting on the strip does (by default the room simply opens it); `now`, when given, is how
 * the reader gets back to what the desk is at.
 */
function fedRoom(ctx, { pick = null, now = null } = {}) {
  const room = meetingRoom({ roster: ctx.roster });
  const problem = el("p", { class: "banner", hidden: true });
  // The day in the room. Read again whenever the room is loaded, and once a minute: the watch goes on
  // between meetings, and its line would otherwise stand still.
  const day = { session: null, asked: 0 };
  const strip = dayStrip({
    onPick: (session, id) => (pick ? pick(session, id) : feed.open({ session, run_id: id })),
    onNow: now,
  });
  async function readDay() {
    const mine = day.asked += 1;
    const answer = await getJSON("/api/day", day.session ? { arm: ctx.arm, day: day.session } : { arm: ctx.arm });
    if (mine !== day.asked || !answer.ok) return;        // asked again since, or not readable just now: keep what is shown
    strip.load(answer.data, room.runId(), Boolean(feed.target().run_id));
  }
  const minutely = window.setInterval(readDay, 60000);
  // The market data of whichever run is in the room. A run's snapshot does not change while it goes on, so it
  // is read once per run; if it could not be read, it is asked for again the next time the room is loaded.
  // The tape along the top goes with the room: it shows the prices of the run in the room, never those of a
  // later session while an earlier one is being played. Until they have been read it shows nothing.
  const market = { run: null, asked: 0 };
  ctx.tape(null);
  async function readMarket() {
    const run = room.runId();
    if (run === market.run) return;
    market.run = run;
    const mine = market.asked += 1;
    if (!run) {
      room.setMarket(null);
      ctx.tape(null);
      return;
    }
    const answer = await getJSON("/api/market", { arm: ctx.arm, run_id: run });
    if (mine !== market.asked) return;                // the room has moved on to another run since
    if (!answer.ok) market.run = null;
    room.setMarket(answer.ok ? answer.data : { available: false, reason: "unreachable" });
    ctx.tape(answer.ok ? answer.data : null);
  }
  const feed = roomFeed({
    room: {
      ...room,
      load: (meeting) => {
        room.load(meeting);
        day.session = meeting.session;
        readMarket();
        readDay();
      },
    },
    live: ctx.live,
    fetchMeeting: (target) => getJSON("/api/meeting", { arm: ctx.arm, ...target }),
    onProblem: (text) => {
      problem.hidden = text === null;
      problem.textContent = text === null ? "" : `⚠ ${text}`;
    },
  });
  const close = () => {
    window.clearInterval(minutely);
    day.asked += 1;
    feed.stop();
    market.asked += 1;
    room.destroy();
  };
  return { room, feed, problem, strip, close };
}

/**
 * What the desk is at now: the meeting that is going, live, or the last one that was held. New lines arrive
 * over the stream and appear by themselves. An earlier meeting of the day can be opened from the strip.
 */
export function roomPage(ctx) {
  const fed = fedRoom(ctx, { now: () => fed.feed.open({}) });
  const { room, feed, problem, strip, close } = fed;
  feed.open({});
  return {
    node: el("div", { class: "stack" }, problem, strip.node, room.node), onStream: feed.hear, refresh: feed.reload,
    destroy: close,
  };
}

// ── 回放 ─────────────────────────────────────────────────────────────────────

/**
 * Any session, played back from its record. Picking a day shows the run that
 * counts for it; the runs page can also send the reader here for one
 * particular run, a failed or replaced attempt included. A run that is still
 * going is followed as in the meeting room.
 */
export function replayPage(ctx) {
  const { room, feed, problem, strip, close } = fedRoom(ctx, { pick: (session, id) => open(session, id) });
  const picker = el("div", { class: "sessions" });
  const node = el("div", { class: "stack" },
    el("h1", { text: t("回放") }),
    card(t("选一个交易日"), t("选一天，放的是当天的收盘会（还没开收盘会的那天，放最近一场）；当天别的会议在下面那一条里选。")
      + t("要看某一次失败或被取代的运行，从“运行状态”页点进来。"), picker),
    problem, strip.node, room.node);
  let sessions = [];
  let started = false;

  function paintPicker() {
    const { session, run_id: run } = feed.target();
    clear(picker);
    if (sessions.length === 0) picker.append(empty(t("还没有可以回放的交易日。")));
    for (const item of sessions) {
      picker.append(el("button", {
        type: "button", "aria-pressed": String(item.session === session && !run), onclick: () => open(item.session),
      }, item.session, el("span", { class: "small muted", text: t`　${word("run", item.status)} · ${item.events} 条` })));
    }
  }
  function open(session, run = null) {
    window.history.replaceState(null, "", run ? `#replay/${session}/${encodeURIComponent(run)}` : `#replay/${session}`);
    const loaded = feed.open(run ? { session, run_id: run } : { session });
    paintPicker();
    return loaded;
  }
  async function refresh() {
    const answer = await getJSON("/api/sessions", { arm: ctx.arm });
    if (!answer.ok) return;
    sessions = answer.data.sessions;
    if (started) {
      paintPicker();
      feed.reload();
    } else if (sessions.length) {
      started = true;
      const asked = sessions.some((item) => item.session === ctx.param);
      const loaded = open(asked ? ctx.param : sessions[0].session, asked ? ctx.run : null);
      // Sent here for one particular line (from the decisions page): go straight to it.
      if (asked && ctx.line) loaded.then((there) => { if (there) room.seek(ctx.line - 1); });
    } else {
      paintPicker();
    }
  }
  refresh();
  return { node, refresh, onStream: feed.hear, destroy: close };
}

// ── 决策 ─────────────────────────────────────────────────────────────────────

/**
 * One ticker on one day: which way the analyst leaned, what was done about it, and whether that differs from the
 * day before. It leads to the replay of the very run the cell was read from. (The address starts with "#replay/",
 * so whatever a run is called it can only lead to this page's own replay.)
 */
function matrixCell(now, before, day) {
  if (!now) return el("span", { class: "cell-none", "aria-label": t("这一场没有涉及"), text: "·" });
  const way = { bullish: "up", bearish: "down" }[now.direction] || null;
  const glyph = { bullish: "▲ ", neutral: "◆ ", bearish: "▼ " }[now.direction] || "";
  const turned = Boolean(before && before.direction && now.direction && before.direction !== now.direction);
  const did = fateText(now);
  return el("a", {
    class: ["cell-box", now.held ? "held" : "", turned ? "changed" : ""].filter(Boolean).join(" "),
    href: `#replay/${day.session}/${encodeURIComponent(day.run_id)}`,
  },
    el("b", { class: way },
      now.direction ? `${glyph}${word("direction", now.direction)} ${num(now.confidence, 2)}` : t("没有观点")),
    did ? el("span", { text: did }) : null,
    now.repeats ? el("span", { text: t`另有 ${now.repeats} 条重复提案不算` }) : null,
    now.held ? el("span", { text: t("当时持仓") }) : null,
    turned ? el("span", { class: "mark", text: t`上一场是${word("direction", before.direction)}` }) : null);
}

function matrix(history) {
  if (history.sessions.length === 0) return empty(t("还没有完成过一场，没有可以比的。"));
  const head = el("tr", {}, el("th", {}),
    history.sessions.map((item) => el("th", { class: "day" }, item.session.slice(5),
      el("small", { text: item.regime
        ? `${word("appetite", item.regime.risk_appetite)} ${num(item.regime.confidence, 2)}` : t("没有宏观结论") }),
      item.halted ? el("small", { text: t("当时停机") }) : null)));
  const rows = history.tickers.map((ticker) => el("tr", {},
    el("th", { class: "name", text: ticker }),
    history.sessions.map((item, at) => el("td", {},
      matrixCell(item.tickers[ticker], at ? history.sessions[at - 1].tickers[ticker] : null, item)))));
  return el("div", { class: "scroll" }, el("table", { class: "matrix" }, el("thead", {}, head), el("tbody", {}, rows)));
}

/**
 * How decisions changed from session to session, and how one session's were
 * made: a grid of every ticker against every finished session, and under it
 * the trail of whichever session is picked.
 */
export function decisionsPage(ctx) {
  const grid = el("div", {});
  const picker = el("div", { class: "sessions" });
  let chosen = null;                                 // the day whose trail is shown, with the run the grid read it from
  let shown = { history: null, meeting: null };
  const trail = decisionTrail({
    // To the same run the grid and the trail were read from, at the line the cell stands for.
    onSeek: (line) => { window.location.hash = `#replay/${chosen.session}/${encodeURIComponent(chosen.run_id)}/${line + 1}`; },
    lead: t("每只股票一行，从左到右是它走过的每一步。点任一格，到回放里看那一句原话。"),
    whole: true,                                     // a finished run, shown as it ended
  });
  const node = el("div", { class: "stack" },
    el("h1", { text: t("决策") }),
    card(t("每只股票的决策是怎么变的"),
      t("一列是一个交易日，日期下面是当天的宏观结论。一格里是分析师的方向和把握度、当天对它做了什么。")
      + t("“风控放行”是订单通过了风控，取自当天的信号表；成交情况在“持仓和订单”页。")
      + t("左边有色条的是当时持仓；框起来并带黑色标记的，是方向和上一场不一样。点一格，回放那一天。"), grid),
    card(t("看哪一场的决策过程"), t("和上面那张表读的是同一次运行：当天算数的那一次。"), picker),
    trail.node);

  async function open(day) {
    chosen = day;
    for (const item of picker.querySelectorAll("button")) item.setAttribute("aria-pressed", String(item.dataset.day === day.session));
    const answer = await getJSON("/api/meeting", { arm: ctx.arm, run_id: day.run_id });
    if (!answer.ok || !answer.data.run || chosen !== day) return;
    const text = JSON.stringify(answer.data.events);
    if (text === shown.meeting) return;
    shown.meeting = text;
    // A run in this grid has finished, so its record is over at its last line.
    trail.load(answer.data.events, answer.data.run.run_id, false);
    trail.update(answer.data.events.length - 1);      // its last line; this trail shows the whole record wherever it is
  }
  async function refresh() {
    const answer = await getJSON("/api/history", { arm: ctx.arm });
    if (!answer.ok) {
      clear(grid).append(empty(answer.data.error || t("读不到数据。")));
      return;
    }
    const days = answer.data.sessions;
    const text = JSON.stringify(answer.data);
    if (text !== shown.history) {
      shown.history = text;
      clear(grid).append(matrix(answer.data));
      clear(picker);
      if (days.length === 0) picker.append(empty(t("还没有完成过一场。")));
      for (const day of days.slice().reverse()) {
        picker.append(el("button", { type: "button", "data-day": day.session, "aria-pressed": "false", onclick: () => open(day), text: day.session }));
      }
    }
    // Keep the day that was picked, read again from the run that stands for it now; else the latest.
    const kept = chosen ? days.find((day) => day.session === chosen.session) : null;
    if (days.length) open(kept || days[days.length - 1]);
  }
  refresh();
  return { node, refresh, destroy: hideTip };
}
