// The desk's plans on the 持仓 page: each condition put into words from its recorded kind and figures, where it
// stands, and what was recorded about it word for word; the versions; the plans that are over; and the lines
// shown when there are none. Run with `node --test`. There is no browser and no server here: both are stand-ins.

import assert from "node:assert/strict";
import { test } from "node:test";

import { all, everything, install, wearing, words } from "./fake_dom.mjs";
import { CONDITIONS, PLANS } from "./plan_fixture.mjs";

install();
const {
  actionText, chancesLine, conditionText, foundText, plansSection, sourceText, stateBadge, stateText, waiting, whoText,
} = await import("../../dashboard/static/js/plans.js");
const { bookPage } = await import("../../dashboard/static/js/pages.js");

const settled = async () => { for (let turn = 0; turn < 5; turn += 1) await new Promise((done) => setImmediate(done)); };
const BOOK = { arm: "desk/fake/a", positions: [], orders: [], fills: [], signals: null };

test("each kind of condition is put into words from its own figures", () => {
  assert.deepEqual(CONDITIONS.map((row) => conditionText(row.condition)), [
    "到 2026-11-30 收盘", "价格跌到 404.65 或以下", "价格涨到 412.40 或以上", "收盘跌破 50 日均线",
    "模拟的『先碰止损』概率高于 60%", "新闻标题出现 subpoena / recall", "模拟的『先到目标』概率低于 20%", "到 2026-11-17 收盘",
  ]);
  assert.equal(conditionText({ kind: "chance_above", ending: "middle", level: 0.5 }), "模拟的『两者都没到』概率高于 50%");
  // A kind this page does not know yet is shown as it was recorded, never guessed at.
  assert.equal(conditionText({ kind: "something_new" }), "something_new");
});

test("each action, who carries it out, and who wrote the plan", () => {
  assert.deepEqual(CONDITIONS.map(actionText), ["卖出全部", "卖出全部", "止损上调到 403.56", "卖出一半", "召集会议", "召集会议", "撤回未成交的买单", "卖出一半"]);
  assert.deepEqual([whoText({ automatic: true }), whoText({ automatic: false })], ["代码自动执行", "开会决定"]);
  // A sale on a day is a time stop: code carries it out only for a trade that is not ahead, and the page says so.
  assert.equal(whoText({ automatic: true, unless_ahead: true }), "代码自动执行，赚钱时改为上会");
  assert.equal(whoText({ automatic: false, unless_ahead: true }), "开会决定");
  assert.deepEqual([sourceText("manager"), sourceText("default")], ["组合经理起草", "系统默认"]);
});

test("each state has its own label and its own mark", () => {
  const names = ["waiting", "fired", "done", "not_done", "heard"];
  assert.deepEqual(names.map(stateText), ["尚未触发", "已触发，待处理", "已执行", "未执行", "已上会"]);
  const marks = names.map((name) => stateBadge(name).attributes.class);
  assert.deepEqual(marks, ["state state-idle", "state state-warning", "state state-good", "state state-critical", "state state-info"]);
  assert.equal(new Set(marks).size, names.length);
});

test("when a condition was found is told by the New York clock, and by whom", () => {
  assert.equal(foundText(CONDITIONS[1].trigger), "2026-11-17 10:00（纽约时间）由盘中巡检发现");
  assert.equal(foundText(CONDITIONS[6].trigger), "2026-11-17 18:30（纽约时间）由收盘会发现");
});

test("a buy still waiting is said so, by the book where it can tell and by the plan otherwise", () => {
  assert.equal(waiting({ held_now: false }, { held: false }), true);
  assert.equal(waiting({ held_now: true }, { held: false }), false);           // the buy filled after the version was made
  assert.equal(waiting({ held_now: false }, { held: true }), false);           // sold since: not a buy waiting
  assert.equal(waiting({ held_now: null }, { held: false }), true);
});

test("a plan that stands: its header, its chances, its conditions and its versions, with what was recorded word for word", () => {
  const section = plansSection(PLANS);
  const [upa, upb] = wearing(section, "plan");
  assert.equal(words(wearing(section, "plan-head")[0]), "UPA第 2 版组合经理起草止损 $400.83期限到 2026-11-30 收盘");
  assert.equal(words(wearing(section, "plan-head")[1]), "UPB第 1 版系统默认止损 $400.83期限到 2026-11-30 收盘买单待成交");
  assert.equal(words(chancesLine(PLANS.standing[0].versions[1])),
    "先到目标 34%先碰止损 21%两者都没到 45%取自 2026-11-17 收盘会的模拟；目标 $431.50 是名义目标。");
  assert.equal(chancesLine(PLANS.standing[0].versions[0]), null);
  assert.equal(chancesLine({ ...PLANS.standing[0].versions[1], target: null }).children.at(-1).textContent, "取自 2026-11-17 收盘会的模拟。");

  const rows = all(upa, (node) => node.tag === "tr").slice(1).map(words);
  assert.equal(rows.length, CONDITIONS.length);
  assert.equal(rows[0], "[time] 到 2026-11-30 收盘每个计划都有的规则卖出全部代码自动执行尚未触发The time the trade was given has run out without it reaching its stop.");
  assert.equal(rows[1], "[wrong] 价格跌到 404.65 或以下卖出全部代码自动执行已触发，待处理Reason for wrong.2026-11-17 10:00（纽约时间）由盘中巡检发现It traded at 402.74.");
  assert.equal(rows[5], "[5] 新闻标题出现 subpoena / recall召集会议开会决定已上会Reason for 5.2026-11-16 10:00（纽约时间）由盘中巡检发现"
    + "A headline has 'subpoena': UPA Corp receives a subpoena over its accounts"
    + "Heard at the called check-in of 2026-11-16 10:05 New York time.");
  assert.equal(rows[7], "[1] 到 2026-11-17 收盘第 1 版之后已去掉卖出一半代码自动执行，赚钱时改为上会已执行Half off after two sessions."
    + "2026-11-17 18:30（纽约时间）由收盘会发现The session of 2026-11-17 has closed.A sale of 12 share(s) is placed for the next open.");

  // The thesis, and every version newest first, with its basis, its changes and what was refused, as recorded.
  assert.ok(everything(upa).includes("The trend carries it."));
  const versions = wearing(upa, "plan-version").map(words);
  assert.equal(versions.length, 2);
  assert.equal(versions[0], "第 2 版2026-11-17组合经理起草"
    + "Its condition [1] held on 2026-11-17: The session of 2026-11-17 has closed. The manager: The trim is done."
    + "Dropped: [1] On the close of 2026-11-17: sell half, by itself. Half off after two sessions."
    + "被拒绝的部分（原文）Condition 2 (chance_below, exit): one of the simulation's chances may call a meeting and nothing else."
    + "数据提醒（原文）UPA: the 50-day average is missing.");
  assert.equal(versions[1], "第 1 版2026-11-13组合经理起草There is no plan for it yet.第一版");
  assert.match(words(all(upa, (node) => node.tag === "summary")[0]), /^版本记录（2 版/);
  assert.ok(everything(upb).includes("The plan is refused whole: it does not say what would show that the plan is wrong."));
});

test("the plans that are over are listed folded away, with why each ended and where its conditions came to", () => {
  const ended = wearing(plansSection(PLANS), "plan-ended")[0];
  assert.equal(ended.tag, "details");
  assert.equal("open" in ended.attributes, false);
  assert.equal(words(all(ended, (node) => node.tag === "summary")[0]), "已结束的计划（1 个）");
  const [row] = all(ended, (node) => node.tag === "tr").slice(1).map(words);
  assert.equal(row, "UPZ2026-11-13 至 2026-11-181[time] 尚未触发[wrong] 已执行The position was sold at the open on 2026-11-18, at 402.54.");
});

test("with no plans, each group says why in one muted line", () => {
  assert.equal(words(wearing(plansSection({ arm: "desk/fake/a", has_plans: false, standing: [], ended: [] }), "empty")[0]),
    "还没有计划。会议室在收盘会上为每笔买入和每只持仓生成计划。");
  assert.equal(words(wearing(plansSection({ arm: "fake/a", has_plans: false, standing: [], ended: [] }), "empty")[0]),
    "这一组不做作战计划。只有会议室一组有。");
  assert.equal(words(wearing(plansSection(null), "empty")[0]), "读不到作战计划。");
  const over = plansSection({ ...PLANS, standing: [] });
  assert.equal(words(wearing(over, "empty")[0]), "现在没有生效的计划。");
  assert.equal(wearing(over, "plan-ended").length, 1);
});

test("the 持仓 page reads the plans beside the book, on every refresh, and puts them below everything else", async () => {
  const asked = [];
  let plans = PLANS;
  globalThis.fetch = async (address) => {
    asked.push(address);
    const data = address.startsWith("/api/plans") ? plans : BOOK;
    return { ok: true, status: 200, json: async () => data };
  };
  const page = bookPage({ arm: "desk/fake/a" });
  await settled();
  assert.deepEqual(asked.sort(), ["/api/book?arm=desk%2Ffake%2Fa", "/api/plans?arm=desk%2Ffake%2Fa"]);
  const titles = () => all(page.node, (node) => node.tag === "h2").map((node) => node.textContent);
  assert.equal(titles().at(-1), "作战计划");
  assert.equal(wearing(page.node, "plan").length, 2);
  plans = { ...PLANS, standing: PLANS.standing.slice(0, 1) };
  await page.refresh();
  assert.equal(asked.length, 4);
  assert.equal(wearing(page.node, "plan").length, 1);
});
