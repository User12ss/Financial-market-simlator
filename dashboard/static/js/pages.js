// The pages. Each one reads from the server and lays out what it got. None
// has a control that changes anything: there is nothing here to press that
// places an order, moves a limit or starts a run.

import {
  el, clear, getJSON, usd, signedUsd, pct, signedPct, rate, num, when, span, bytes, word, delta, state, runState,
  tile, card, empty, table, DASH,
} from "./dom.js";
import { lineChart, rateBars, quantileStrip, meter } from "./charts.js";
import { roomFeed } from "./live.js";
import { meetingRoom } from "./meeting.js";

const SERIES = { account: "var(--series-1)", benchmark: "var(--series-2)", candidates: "var(--series-3)" };
const money = (value, short) => usd(value, short ? 0 : 2);

/** A page that shows one answer from the server and repaints only when the answer has changed. */
function dataPage(ctx, path, paint) {
  const warning = el("p", { class: "banner", hidden: true });
  const body = el("div", { class: "stack" });
  const node = el("div", { class: "stack" }, warning, body);
  let last = null;                                   // what is on the page now, when it was read successfully
  async function refresh() {
    const answer = await getJSON(path, { arm: ctx.arm });
    if (answer.status === 0 && last !== null) {
      // The server cannot be reached. What was last read stays up, marked as such, rather than vanishing.
      warning.textContent = `⚠ ${answer.data.error}下面是最后一次读到的内容，可能已经过时。`;
      warning.hidden = false;
      return;
    }
    warning.hidden = true;
    const text = JSON.stringify(answer.data);
    if (answer.ok && text === last) return;
    last = answer.ok ? text : null;
    clear(body);
    if (answer.ok) paint(body, answer.data);
    else body.append(empty(answer.data.error || "读不到数据。"));
  }
  refresh();
  return { node, refresh };
}

// ── 总览 ─────────────────────────────────────────────────────────────────────

function guards(data) {
  const { kill_switch: halt, daily_loss: day, drawdown: fall } = data.guards;
  const line = (name, tripped, bar) => el("div", { class: "stack-tight" },
    el("div", { class: "row spread" },
      el("strong", { text: name }), state(tripped ? "critical" : "good", tripped ? "已触发：不开新仓" : "未触发")),
    bar);
  return card("熔断和停机开关", "这些由代码判断，模型改不了，这个页面也改不了。",
    el("div", { class: "stack" },
      el("div", { class: "stack-tight" },
        el("div", { class: "row spread" },
          el("strong", { text: "停机开关" }),
          state(halt.on ? "critical" : "good", halt.on ? "已启用：不下任何新订单" : "没有启用")),
        halt.on ? el("p", { class: "small said", text: `原因：${halt.reason}` }) : null,
        el("p", { class: "small muted" },
          "这是看板此刻看到的状态。开关是这个文件，存在即停机：", el("code", { text: halt.file }),
          "。只给流程单独设置的环境变量 TRADING_HALT，看板看不到。"),
        // What the pipeline itself wrote down the last time it finished: this part is the record.
        halt.last_run ? el("p", { class: "small" },
          `最近完成的一场（${halt.last_run.session}）自己记下的：`,
          halt.last_run.halted ? el("span", { class: "said", text: halt.last_run.halted }) : "当时没有停机。") : null),
      line("当日亏损熔断", day.tripped, meter({
        name: "当日净值变化", valueText: day.value_pct === null ? "还没有数据" : signedPct(day.value_pct),
        limitText: `−${day.limit_pct}%`, share: day.value_pct !== null && day.value_pct < 0 ? -day.value_pct / day.limit_pct : 0,
        tripped: day.tripped,
      })),
      line("最大回撤熔断", fall.tripped, meter({
        name: "距最高点的回撤", valueText: fall.value_pct === null ? "还没有数据" : pct(fall.value_pct),
        limitText: `${fall.limit_pct}%`, share: fall.value_pct === null ? 0 : fall.value_pct / fall.limit_pct,
        tripped: fall.tripped,
      }))));
}

function paintOverview(node, data) {
  const account = data.account;
  node.append(el("div", { class: "row spread" },
    el("h1", { text: "总览" }),
    el("div", { class: "row" },
      el("span", { class: "small muted", text: data.latest_run ? `最近一次运行：${data.latest_run.session}` : "" }),
      runState(data.latest_run))));
  if (!account) {
    node.append(empty("这套模型配置还没有模拟账户：流程还没有走到下单那一步。"));
    return;
  }
  node.append(el("div", { class: "kpis" },
    tile("账户净值", usd(account.equity), `截至 ${account.as_of || DASH} 收盘`, true),
    tile("累计收益", delta(account.return_pct, signedPct(account.return_pct)), `初始资金 ${usd(account.initial_cash, 0)}`),
    tile("当日盈亏", delta(account.day_change_pct, signedPct(account.day_change_pct)),
      account.day_change_pct === null ? "只有一个交易日，还算不出" : "相对上一个交易日"),
    tile("现金", usd(account.cash), account.equity ? `占净值 ${pct((account.cash / account.equity) * 100, 1)}` : null),
    tile("持仓市值", usd(account.positions_value), `${data.positions} 只持仓，${data.open_orders} 张订单等待成交`),
    tile("距最高点回撤", pct(account.drawdown_pct), `最高点 ${usd(account.high_water)}`)));

  const series = data.curves.map((curve) => ({ name: curve.name, color: SERIES[curve.key], points: curve.points }));
  const about = data.evaluated_as_of
    ? `两条基准线取自 ${data.evaluated_as_of} 的评估报告。第三个基准（随机挑选）是一个分布，不是一条线，在“评估”页。`
    : "还没有评估报告，所以只有账户自己这一条线。评估跑过一次之后，这里会多出两条基准线。";
  node.append(el("div", { class: "grid cols-2" },
    card("净值和基准", about, lineChart({ series, valueText: money, label: "账户净值和基准的走势" })),
    guards(data)));
}

export const overviewPage = (ctx) => dataPage(ctx, "/api/overview", paintOverview);

// ── 持仓和订单 ───────────────────────────────────────────────────────────────

function paintBook(node, data) {
  node.append(el("h1", { text: "持仓和订单" }));
  node.append(card("持仓", "市值按最近一个收盘价算。", table([
    { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
    { title: "行业", cell: (row) => row.sector },
    { title: "股数", num: true, cell: (row) => num(row.shares) },
    { title: "成本价", num: true, cell: (row) => usd(row.avg_cost) },
    { title: "最近收盘", num: true, cell: (row) => usd(row.last_close) },
    { title: "市值", num: true, cell: (row) => usd(row.value) },
    { title: "浮动盈亏", num: true, cell: (row) => delta(row.unrealized_usd, `${signedUsd(row.unrealized_usd)}（${signedPct(row.unrealized_pct)}）`) },
    { title: "止损价", num: true, cell: (row) => usd(row.stop_price) },
    { title: "买入日", cell: (row) => row.entry_date },
    { title: "持有交易日", num: true, cell: (row) => num(row.sessions_held) },
  ], data.positions, "现在没有持仓。")));

  node.append(card("挂着的订单", "还没有成交的订单，包括每个持仓的保护性止损。", table([
    { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
    { title: "方向", cell: (row) => word("side", row.side) },
    { title: "类型", cell: (row) => word("kind", row.kind) },
    { title: "股数", num: true, cell: (row) => num(row.shares) },
    { title: "限价", num: true, cell: (row) => usd(row.limit_price) },
    { title: "止损价", num: true, cell: (row) => usd(row.stop_price ?? row.protective_stop) },
    { title: "下单的交易日", cell: (row) => row.created_session },
    { title: "下单理由（原文）", said: true, cell: (row) => row.reason },
  ], data.orders, "现在没有挂着的订单。")));

  node.append(card("最近的成交", "最新的在最上面。", table([
    { title: "成交日", cell: (row) => row.fill_date },
    { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
    { title: "方向", cell: (row) => word("side", row.side) },
    { title: "类型", cell: (row) => word("kind", row.kind) },
    { title: "股数", num: true, cell: (row) => num(row.shares) },
    { title: "成交价", num: true, cell: (row) => usd(row.price, 4) },
    { title: "相对成本", num: true, cell: (row) => (row.pnl_pct === null ? null : delta(row.pnl_pct, signedPct(row.pnl_pct))) },
    { title: "备注（原文）", said: true, cell: (row) => row.note },
  ], data.fills, "还没有成交。")));

  const signals = data.signals;
  if (!signals) {
    node.append(card("最近一场的候选和信号", null, empty("还没有完成过一场。")));
    return;
  }
  const regime = signals.regime;
  node.append(card(`${signals.session} 这一场的候选和信号`, "最近一场完成的流程留下的结果。",
    el("div", { class: "kpis" },
      tile("这一场", word("run", signals.status), signals.halted ? "当时处于停机状态" : null),
      tile("股票池", `${num(signals.universe_size)} 只`, `筛出 ${signals.candidates.length} 只候选`),
      tile("宏观风险偏好", regime ? word("appetite", regime.risk_appetite) : DASH,
        regime ? `把握度 ${num(regime.confidence, 2)}，形成于 ${regime.formed_on}` : "这一场没有宏观结论")),
    el("p", { class: "small" }, "候选：", signals.candidates.length
      ? signals.candidates.map((ticker) => el("span", { class: "chip", text: ticker }))
      : el("span", { class: "muted", text: "没有" })),
    signals.warnings.length ? el("div", {},
      el("h3", { text: "这一场记下的提醒（原文）" }),
      el("ul", { class: "plain said" }, signals.warnings.map((text) => el("li", { text })))) : null));

  node.append(card("分析师的观点", "每只候选和持仓各一条。", table([
    { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
    { title: "已持有", cell: (row) => (row.held ? "是" : "否") },
    { title: "方向", cell: (row) => word("direction", row.direction) },
    { title: "把握度", num: true, cell: (row) => num(row.confidence, 2) },
    { title: "期限（交易日）", num: true, cell: (row) => num(row.horizon_days) },
    { title: "建议止损", num: true, cell: (row) => usd(row.proposed_stop) },
    { title: "论点（原文）", said: true, cell: (row) => row.thesis },
  ], signals.views, "这一场没有观点。")));

  node.append(card("组合经理的提案，和规则检查的结果", null, table([
    { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
    { title: "动作", cell: (row) => word("action", row.action) },
    { title: "把握度", num: true, cell: (row) => num(row.confidence, 2) },
    { title: "检查结果", cell: (row) => word("check", row.status) },
    { title: "理由（原文）", said: true, cell: (row) => row.reason },
    { title: "检查备注（原文）", said: true, cell: (row) => row.note },
  ], signals.proposals, "这一场没有提案。")));

  node.append(card("风控对每张订单的决定", null, table([
    { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
    { title: "方向", cell: (row) => word("side", row.side) },
    { title: "申请股数", num: true, cell: (row) => num(row.requested_shares) },
    { title: "批准股数", num: true, cell: (row) => num(row.approved_shares) },
    { title: "结论", cell: (row) => word("verdict", row.verdict) },
    { title: "原因（原文）", said: true, cell: (row) => (row.reasons || []).join(" ") },
  ], signals.risk, "这一场没有订单送到风控。")));
}

export const bookPage = (ctx) => dataPage(ctx, "/api/book", paintBook);

// ── 评估 ─────────────────────────────────────────────────────────────────────

const tallyColumns = (first, group = null) => [
  { title: first, cell: (row) => (group ? word(group, row.label) : row.label) },
  { title: "有方向的观点", num: true, cell: (row) => num(row.views) },
  { title: "方向对了", num: true, cell: (row) => `${num(row.right)}（${rate(row.hit_rate)}）` },
  { title: "跑赢基准", num: true, cell: (row) => `${num(row.beat_benchmark)}（${rate(row.beat_rate)}）` },
  { title: "平均超额", num: true, cell: (row) => signedPct(row.mean_edge_pct) },
  { title: "平均自报把握度", num: true, cell: (row) => num(row.mean_confidence, 2) },
  { title: "误差范围", num: true, cell: (row) => (row.margin === null ? null : `±${Math.round(row.margin * 100)} 个点`) },
];

const sliceColumns = (first) => [
  { title: first, cell: (row) => row.label },
  { title: "笔数", num: true, cell: (row) => num(row.trades) },
  { title: "盈亏", num: true, cell: (row) => delta(row.pnl_usd, signedUsd(row.pnl_usd)) },
];

function paintScorecard(node, report, benchmark) {
  const score = report.scorecard;
  const overall = score.overall;
  node.append(card("观点的成绩", `每条观点从下一个交易日开盘算到期限结束的收盘，和 ${benchmark} 同期比，含分红。`,
    el("div", { class: "kpis" },
      tile("观点总数", num(score.total), `${num(score.pending)} 条还没到期，${num(score.unpriced)} 条算不出价格`),
      tile("已经评分", num(score.scored), `来自 ${num(score.sessions)} 个交易日，其中中性 ${num(score.neutral)} 条`),
      tile("方向命中率", overall ? rate(overall.hit_rate) : DASH,
        overall && overall.margin !== null ? `误差范围约 ±${Math.round(overall.margin * 100)} 个点` : "还没有可评分的观点"),
      tile("跑赢基准的比例", overall ? rate(overall.beat_rate) : DASH,
        overall ? `平均超额 ${signedPct(overall.mean_edge_pct)}` : null)),
    score.scored === 0 ? empty("观点都还没到期，现在没有成绩可看。到期两个交易日之后才会评分。") : null));
  if (score.scored === 0) return;

  node.append(card("把握度准不准", "模型说自己有多大把握，和实际对了多少。条是实际命中率，竖线是这一档里模型自报的平均把握度。",
    score.by_confidence.length
      ? el("div", {},
        rateBars({
          rows: score.by_confidence.map((row) => ({
            label: `把握度 ${row.label.replace(" to ", "–")}`, value: row.hit_rate, marker: row.mean_confidence,
            note: `${row.views} 条观点`,
          })),
          barName: "实际命中率", markerName: "自报把握度（平均）", label: "各档把握度的实际命中率",
        }),
        el("details", {}, el("summary", { text: "查看数据表" }), table(tallyColumns("把握度"), score.by_confidence)))
      : empty("还没有按把握度分档的成绩。")));
  node.append(el("div", { class: "grid cols-2" },
    card("按方向", null, table(tallyColumns("方向", "direction"), score.by_direction)),
    card("按是否已持有", null, table(tallyColumns("类别", "position"), score.by_position)),
    card("按行业", null, table(tallyColumns("行业"), score.by_sector)),
    card("按当时的宏观环境", null, table(tallyColumns("风险偏好", "appetite"), score.by_regime))));
}

function paintTrades(node, report) {
  const trades = report.trades;
  node.append(card("交易的结果", "一笔交易从买入算到全部卖出。",
    el("div", { class: "kpis" },
      tile("已平仓", `${num(trades.closed)} 笔`, `赢 ${num(trades.winners)}，亏 ${num(trades.losers)}，止损出场 ${num(trades.stopped_out)}`),
      tile("胜率", rate(trades.win_rate), trades.closed < 20 ? "笔数太少，只能当参考" : null),
      tile("已实现盈亏", delta(trades.realized_usd, signedUsd(trades.realized_usd)),
        `平均赢 ${usd(trades.average_win_usd)}，平均亏 ${usd(trades.average_loss_usd)}`),
      tile("还开着的仓位", `${num(trades.open)} 笔`, `浮动盈亏 ${signedUsd(trades.unrealized_usd)}`)),
    table([
      { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
      { title: "行业", cell: (row) => row.sector },
      { title: "决定日", cell: (row) => row.decided_on },
      { title: "买入日", cell: (row) => row.entry_date },
      { title: "买入价", num: true, cell: (row) => usd(row.entry_price, 4) },
      { title: "股数", num: true, cell: (row) => num(row.shares) },
      { title: "状态", cell: (row) => (row.stopped ? "止损出场" : word("trade", row.status)) },
      { title: "已实现", num: true, cell: (row) => delta(row.realized_usd, signedUsd(row.realized_usd)) },
      { title: "浮动", num: true, cell: (row) => (row.unrealized_usd === null ? null : delta(row.unrealized_usd, signedUsd(row.unrealized_usd))) },
      { title: "持有交易日", num: true, cell: (row) => num(row.sessions_held) },
    ], trades.trades, "还没有交易。")));
  if (trades.trades.length) {
    node.append(el("div", { class: "grid cols-2" },
      card("按标的", null, table(sliceColumns("标的"), trades.by_ticker)),
      card("按行业", null, table(sliceColumns("行业"), trades.by_sector)),
      card("按出场方式", null, table(sliceColumns("出场方式（原文）"), trades.by_exit))));
  }
}

function paintRandom(node, report) {
  const random = report.random;
  if (!random.entries) {
    node.append(card("和随机挑选比", null, empty("账户还没有买入过，没有可比的。")));
    return;
  }
  node.append(card("和随机挑选比",
    `账户的 ${random.entries} 笔买入，每一笔都换成当天候选里随机的一只，用同一套规则重放 ${num(random.simulations)} 次，估值到 ${random.valued_on}。`,
    el("div", { class: "kpis" },
      tile("账户好于多少随机结果", pct(random.percentile, 1), random.entries < 30 ? "买入笔数太少，这个位置多半是运气" : null),
      tile("账户（按同一规则重放）", signedUsd(random.replayed_pnl_usd), `账本上记的是 ${signedUsd(random.booked_pnl_usd)}`),
      tile("随机挑选的平均", signedUsd(random.mean_pnl_usd), `中位数 ${signedUsd(random.quantiles_usd["50"])}`)),
    quantileStrip({
      quantiles: random.quantiles_usd, value: random.replayed_pnl_usd, valueName: "这个账户",
      valueText: (value, short) => signedUsd(value, short ? 0 : 2), label: "账户的盈亏在随机挑选结果里的位置",
    }),
    el("h3", { text: "每一笔买入，在当天候选里排第几" }),
    table([
      { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
      { title: "决定日", cell: (row) => row.decided_on },
      { title: "买入日", cell: (row) => row.entry_date },
      { title: "重放盈亏", num: true, cell: (row) => delta(row.replayed_usd, signedUsd(row.replayed_usd)) },
      { title: "账本盈亏", num: true, cell: (row) => signedUsd(row.booked_usd) },
      { title: "当天候选的平均", num: true, cell: (row) => signedUsd(row.pool_mean_usd) },
      { title: "排名", num: true, cell: (row) => `第 ${row.rank} / ${row.pool}` },
    ], random.picks)));
}

function paintEvaluation(node, data, ctx) {
  node.append(el("h1", { text: "评估" }));
  if (!data.as_of) {
    node.append(card("还没有评估报告", null,
      empty("评估是单独运行的，这个页面只读它上一次写下的报告，不会自己去跑。", el("br"), el("code", { text: "python -m evaluation" }))));
  } else {
    node.append(el("p", { class: "small muted" },
      `报告评估到 ${data.as_of}，写于 ${when(data.created_at)}。这个页面只读这份报告，不会自己重新评估。文件：`,
      el("code", { text: data.file })));
    node.append(card("各套模型配置对比", "同一段时间，每套配置各自的账户。", table([
      { title: "模型配置", cell: (row) => (row.arm === ctx.arm ? el("strong", { text: row.arm }) : row.arm) },
      { title: "交易日", num: true, cell: (row) => num(row.sessions) },
      { title: "收益", num: true, cell: (row) => delta(row.return_pct, signedPct(row.return_pct)) },
      { title: "已评分观点", num: true, cell: (row) => num(row.scored) },
      { title: "方向命中率", num: true, cell: (row) => rate(row.hit_rate) },
      { title: "跑赢基准", num: true, cell: (row) => rate(row.beat_rate) },
      { title: "已平仓", num: true, cell: (row) => num(row.closed) },
      { title: "好于多少随机结果", num: true, cell: (row) => (row.buys ? pct(row.percentile, 1) : null) },
      { title: "模型调用", num: true, cell: (row) => num(row.calls) },
      { title: "费用", num: true, cell: (row) => (row.cost_usd === null ? "本地模型，不计费" : usd(row.cost_usd, 4)) },
    ], data.arms)));
  }

  const report = data.report;
  if (report) {
    node.append(card("账户和两条基准", `${report.first_session || DASH} 到 ${report.last_session || DASH}，共 ${report.sessions_run} 个交易日。`,
      table([
        { title: "曲线", cell: (row) => row.title },
        { title: "收益", num: true, cell: (row) => delta(row.return_pct, signedPct(row.return_pct)) },
        { title: "最大回撤", num: true, cell: (row) => pct(row.max_drawdown_pct) },
        { title: "报告里的说明（原文）", said: true, cell: (row) => row.description },
      ], report.curves.map((curve, index) => ({
        ...curve, title: ["模拟账户", `${data.benchmark} 买入持有`, "候选池等权"][index] || curve.name,
      }))),
      report.notes.length ? el("div", {},
        el("h3", { text: "报告自己写的提醒（原文）" }),
        el("ul", { class: "plain said" }, report.notes.map((text) => el("li", { text })))) : null));
    paintScorecard(node, report, data.benchmark);
    paintTrades(node, report);
    paintRandom(node, report);
    const usage = report.usage;
    node.append(card("模型用量", "这套配置到评估日为止的累计。", el("div", { class: "kpis" },
      tile("调用次数", num(usage.calls)),
      tile("输入 / 输出 token", `${num(usage.input_tokens)} / ${num(usage.output_tokens)}`),
      tile("模型用时", span(usage.model_seconds)),
      tile("费用", usage.cost_usd === null ? "不计费" : usd(usage.cost_usd, 4), usage.cost_usd === null ? "本地模型" : null))));
  } else if (data.as_of) {
    node.append(empty("这份报告里没有这套模型配置。"));
  }

  node.append(card("复盘写下的教训",
    data.lessons_shown ? "现在的设置：分析师写观点时会看到这些教训。" : "现在的设置：这些教训只是存着，没有给分析师看。",
    table([
      { title: "标的", cell: (row) => el("strong", { text: row.ticker }) },
      { title: "观点日", cell: (row) => row.session },
      { title: "可用于", cell: (row) => `${row.known_on} 之后` },
      { title: "通过检查", cell: (row) => (row.clean ? "是" : "否") },
      { title: "结果（原文）", said: true, cell: (row) => row.outcome },
      { title: "评价（原文）", said: true, cell: (row) => row.assessment },
      { title: "教训（原文）", said: true, cell: (row) => row.lesson },
    ], data.lessons, "还没有写下任何教训。")));
}

export const evaluationPage = (ctx) => dataPage(ctx, "/api/evaluation", (node, data) => paintEvaluation(node, data, ctx));

// ── 运行状态 ─────────────────────────────────────────────────────────────────

function paintStatus(node, data) {
  const totals = data.totals;
  node.append(el("h1", { text: "运行状态" }));
  node.append(el("div", { class: "kpis" },
    tile("运行次数", num(totals.runs), `失败 ${num(totals.failed || 0)}，降级 ${num(totals.degraded || 0)}`),
    tile("模型调用", num(totals.calls || 0)),
    tile("输入 / 输出 token", `${num(totals.tokens_in || 0)} / ${num(totals.tokens_out || 0)}`),
    tile("模型用时", span(totals.seconds || 0)),
    tile("费用", totals.cost === null ? "不计费" : usd(totals.cost, 4), totals.cost === null ? "本地模型" : null)));
  node.append(card("每一次运行",
    "最新的在最上面。同一个交易日重跑时，之前那次标为“已被重跑取代”。“中断”是看板的判断：账本里还写着运行中，但那个进程已经不在了。", table([
    { title: "交易日", cell: (row) => row.session },
    { title: "状态", cell: (row) => runState(row) },
    { title: "开始", cell: (row) => when(row.started_at) },
    { title: "用时", num: true, cell: (row) => span(row.seconds) },
    { title: "模型调用", num: true, cell: (row) => num(row.llm_calls) },
    { title: "输入 / 输出 token", num: true, cell: (row) => `${num(row.input_tokens)} / ${num(row.output_tokens)}` },
    { title: "费用", num: true, cell: (row) => (row.cost_usd === null ? DASH : usd(row.cost_usd, 4)) },
    { title: "数据快照", cell: (row) => (row.snapshot ? (row.snapshot.exists ? "在" : "文件不见了") : "没有") },
    // The address starts with "#replay/", so whatever a run is called it can only ever lead to this page's own replay.
    { title: "发言", cell: (row) => el("a", { href: `#replay/${row.session}/${encodeURIComponent(row.run_id)}`, text: "回放这一次" }) },
    { title: "提醒和错误（原文）", said: true, cell: (row) => [row.error, ...row.warnings].filter(Boolean).join("\n") || null },
  ], data.runs, "还没有运行记录。")));
  node.append(card("账本", "看板以只读方式打开这个文件，不会写它。",
    el("p", {}, el("code", { text: data.journal.path }), `　${bytes(data.journal.bytes)}`)));
}

export const statusPage = (ctx) => dataPage(ctx, "/api/status", paintStatus);

// ── 会议室 ───────────────────────────────────────────────────────────────────

/** A room and what keeps it in step with the server (the rules are in live.js), with a place to say what went wrong. */
function fedRoom(ctx) {
  const room = meetingRoom({ roster: ctx.roster });
  const problem = el("p", { class: "banner", hidden: true });
  const feed = roomFeed({
    room, live: ctx.live,
    fetchMeeting: (target) => getJSON("/api/meeting", { arm: ctx.arm, ...target }),
    onProblem: (text) => {
      problem.hidden = text === null;
      problem.textContent = text === null ? "" : `⚠ ${text}`;
    },
  });
  return { room, feed, problem, close: () => { feed.stop(); room.destroy(); } };
}

/** The latest session, live while its run is going: new lines arrive over the stream and appear by themselves. */
export function roomPage(ctx) {
  const { room, feed, problem, close } = fedRoom(ctx);
  feed.open({});
  return { node: el("div", { class: "stack" }, problem, room.node), onStream: feed.hear, refresh: feed.reload, destroy: close };
}

// ── 回放 ─────────────────────────────────────────────────────────────────────

/**
 * Any session, played back from its record. Picking a day shows the run that
 * counts for it; the runs page can also send the reader here for one
 * particular run, a failed or replaced attempt included. A run that is still
 * going is followed as in the meeting room.
 */
export function replayPage(ctx) {
  const { room, feed, problem, close } = fedRoom(ctx);
  const picker = el("div", { class: "sessions" });
  const node = el("div", { class: "stack" },
    el("h1", { text: "回放" }),
    card("选一个交易日", "选一天，放的是当天算数的那一次运行。要看某一次失败或被取代的运行，从“运行状态”页点进来。", picker),
    problem, room.node);
  let sessions = [];
  let started = false;

  function paintPicker() {
    const { session, run_id: run } = feed.target();
    clear(picker);
    if (sessions.length === 0) picker.append(empty("还没有可以回放的交易日。"));
    for (const item of sessions) {
      picker.append(el("button", {
        type: "button", "aria-pressed": String(item.session === session && !run), onclick: () => open(item.session),
      }, item.session, el("span", { class: "small muted", text: `　${word("run", item.status)} · ${item.events} 条` })));
    }
  }
  function open(session, run = null) {
    window.history.replaceState(null, "", run ? `#replay/${session}/${encodeURIComponent(run)}` : `#replay/${session}`);
    feed.open(run ? { session, run_id: run } : { session });
    paintPicker();
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
      open(asked ? ctx.param : sessions[0].session, asked ? ctx.run : null);
    } else {
      paintPicker();
    }
  }
  refresh();
  return { node, refresh, onStream: feed.hear, destroy: close };
}
