// The meeting room: fourteen figures round a table and one session's record
// played back in the order it was recorded. Above it, where the meeting ended
// and the stages it went through. In it, a light follows whoever is speaking;
// each line is thrown across the room to the one it was said to, or goes
// round to everyone; a sign over each figure says what it has concluded so
// far. Beside it, who has spoken to whom. Below, the decision trail shows how
// far each ticker has got. The room never invents a line: with no record it
// is empty, and everything it shows is read off the record.

import { el, svg, clear, fill, clock, num, span, when, word, runState, showTip, hideTip, DASH } from "./dom.js";
import { drawFigure, portrait, seats } from "./figures.js";
import { emblem } from "./emblems.js";
import * as playback from "./playback.js";
import { buildTrail, lastSaid, standings, stretches } from "./decisions.js";
import { citationText, readings } from "./market.js";
import { marketScreen } from "./screen.js";
import { decisionTrail } from "./trail.js";
import { calm, replay, typeOut } from "./motion.js";
import { meetingBrief, stageRail } from "./brief.js";
import { EVERYONE } from "./links.js";
import { talkNetwork } from "./network.js";
import { marketWall, statusStrip } from "./backdrop.js";
import { debateBoard, teamBoard } from "./team.js";
import { t } from "./i18n.js";

const ROOM = { width: 1000, height: 680, cx: 500, cy: 352, rx: 400, ry: 232 };
const TABLE = { rx: 292, ry: 150 };
const RIM = { rx: TABLE.rx + 16, ry: TABLE.ry + 16 };          // where the stream between speakers runs
// The market screen's place in the room, in the room's own units: a speaker with no figure speaks from its edge.
const SCREEN = { left: 260, right: 740, top: 0.2425 * 680, bottom: 0.6775 * 680 };
const WALL = { x: 44, y: 30, width: 884, height: 262 };         // the back wall of the room, where the market is drawn
const LEAD = { x: 500, y: 648 };                                // the reader's own place, at the front of the room
const SPEEDS = [0.5, 1, 2, 4];
const ARROW = { up: "▲", down: "▼", flat: "" };
let rooms = 0;                                                  // so that each room's gradients have names of their own

// Chinese names for the recorded fields. A field without one is shown under its recorded name.
const FIELDS = {
  ticker: t("标的"), direction: t("方向"), confidence: t("把握度"), horizon_days: t("期限（交易日）"), thesis: t("论点"),
  catalysts: t("催化因素"), evidence: t("依据"), invalidation: t("什么情况下作废"), held: t("是否已持有"), session_date: t("交易日"),
  data_warnings: t("数据提醒"), stance: t("立场"), summary: t("摘要"), key_risk: t("主要风险"), sector_tilts: t("行业倾向"),
  agent_name: t("记录的角色名"), side: t("一方"), round: t("第几轮"), argument: t("论述"), strongest_evidence: t("最有力的依据"),
  trigger: t("为什么辩论"), reasoning: t("裁决理由"), proposals: t("提案"), action: t("动作"), reason: t("理由"), source: t("来源"),
  status: t("状态"), note: t("备注"), request: t("申请的订单"), decision: t("风控的决定"), order_id: t("订单号"), kind: t("订单类型"),
  shares: t("股数"), price: t("成交价"), fill_date: t("成交日"), pnl_pct: t("相对成本的盈亏（%）"), limit_price: t("限价"),
  stop_price: t("止损价"), protective_stop: t("成交后挂的止损"), day_only: t("仅当日有效"), created_session: t("下单的交易日"),
  filled_price: t("成交价"), filled_date: t("成交日"), risk_appetite: t("风险偏好"), key_risks: t("主要风险"),
  assessments: t("各分析师的评估"), requested_shares: t("申请股数"), approved_shares: t("批准股数"), verdict: t("结论"),
  reasons: t("原因"), tilt: t("倾向"), sector: t("行业"), supporters: t("支持者"), proposed_stop: t("分析师建议的止损"),
  stop_reason: t("止损理由"),
  // What the desk's check-ins record.
  since: t("上一场会议的截止时刻"), as_of: t("这一场的截止时刻"), book: t("账上的股票"), affected: t("有新情况"),
  waiting: t("等待成交的订单"), at_open: t("今天开盘应已成交、尚未入账的订单"), sold_at_open: t("今天开盘应已卖出，不再复盘"),
  new_items: t("新消息"), not_shown: t("没有列出的新消息条数"), trading: t("当天盘中报价"),
  number: t("编号"), item_id: t("台账里的编号"), title: t("标题"), publisher: t("来源"), published_at: t("来源标注的时间"),
  first_seen: t("本系统首次取得的时间"), session: t("交易日"), seen_at: t("取得的时间"), last: t("最新价"),
  previous_close: t("前一个收盘价"), change_pct: t("当日涨跌（%）"), usual_range_pct: t("日常波幅（%）"),
  beyond_usual: t("超出日常波幅"), effect: t("对原观点的影响"), cited: t("引用的新消息"), decisions: t("决定"),
  at: t("时间"), what: t("取的是什么"), error: t("错误"),
};
const HIDDEN = new Set(["remark", "agent_id"]);          // the remark is already in the middle of the room

/** A recorded value, laid out for reading. Text is passed on untouched. */
function shown(value) {
  if (value === null || value === undefined || value === "") return el("span", { class: "muted", text: DASH });
  if (typeof value === "boolean") return value ? t("是") : t("否");
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return el("span", { class: "muted", text: t("无") });
    if (value.every((item) => typeof item !== "object" || item === null)) {
      return el("ul", { class: "plain" }, value.map((item) => el("li", { text: String(item) })));
    }
    return el("div", { class: "nested" }, value.map((item) => fields(item)));
  }
  return fields(value);
}

function fields(record) {
  const list = el("dl", {});
  for (const [key, value] of Object.entries(record)) {
    if (HIDDEN.has(key)) continue;
    list.append(el("dt", { text: FIELDS[key] || key, title: key }), el("dd", {}, shown(value)));
  }
  return list;
}

/** Where a seat is round the table, as an angle. */
const angleOf = (seat) => Math.atan2((seat.y - ROOM.cy) / ROOM.ry, (seat.x - ROOM.cx) / ROOM.rx);
const onRim = (angle) => [ROOM.cx + RIM.rx * Math.cos(angle), ROOM.cy + RIM.ry * Math.sin(angle)];
const feet = (seat) => [seat.x, seat.y - 4];
const chest = (seat) => [seat.x, seat.y - 62 * seat.scale];
const point = ([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`;

/** A line thrown from one place to another: it always rises on the way, further the longer the throw. */
export function throwPath([x0, y0], [x1, y1]) {
  const length = Math.hypot(x1 - x0, y1 - y0) || 1;
  let nx = -(y1 - y0) / length, ny = (x1 - x0) / length;
  // Of the two sides it could bend to, the upper one; straight up and down, the side away from the middle.
  if (ny > 1e-9 || (Math.abs(ny) <= 1e-9 && nx * ((x0 + x1) / 2 - ROOM.cx) < 0)) {
    nx = -nx;
    ny = -ny;
  }
  const lift = Math.min(150, 18 + 0.26 * length);
  return `M${point([x0, y0])} Q${point([(x0 + x1) / 2 + nx * lift, (y0 + y1) / 2 + ny * lift])} ${point([x1, y1])}`;
}

/** The table's edge from one angle on, for `sweep` radians, as line segments. */
function along(from, sweep) {
  const count = Math.max(6, Math.ceil(Math.abs(sweep) / 0.07));
  const steps = [];
  for (let step = 0; step <= count; step += 1) steps.push(`L${point(onRim(from + (sweep * step) / count))}`);
  return steps.join(" ");
}

export function meetingRoom({ roster }) {
  const byId = new Map(roster.map((persona) => [persona.id, persona]));
  const places = seats(roster.length, ROOM);
  const seatOf = (id) => places[byId.get(id).seat];
  const state = { ...playback.newPlayback(), picked: null, only: false, timer: null, market: null };
  const room = `room${rooms += 1}`;
  const screen = marketScreen({ personas: byId });

  // ── The room itself ────────────────────────────────────────────────────────
  const spot = svg("circle", { class: "spot", r: 190, cx: 0, cy: 0, fill: `url(#${room}-spot)` });
  const wall = marketWall(WALL);
  // The reader leads this team: their place is at the front of the room, facing it, and what is said to everyone is said to them too.
  const lead = svg("g", { class: "lead", transform: `translate(${LEAD.x},${LEAD.y})`, "aria-label": t("负责人席：你的位置") },
    svg("path", { class: "lead-desk", d: "M-92,-10 L92,-10 L104,10 L-104,10 Z" }),
    svg("rect", { class: "lead-light", x: -92, y: -11.5, width: 184, height: 1.6 }),
    svg("text", { class: "lead-name", x: 0, y: 4.5, "text-anchor": "middle", text: t("负责人席 · 你") }));
  const flow = svg("g", { class: "flow" });
  // Over everything else in the room, and never in the way of a pointer: the line being said, on its way across.
  const links = svg("svg", { class: "links", viewBox: `0 0 ${ROOM.width} ${ROOM.height}`, "aria-hidden": "true" });
  // Whoever speaks without a figure of its own (the pipeline's voice, the seats that are code) speaks through the
  // screen: its name hangs from the screen's upper edge while it does, and its line leaves from the screen's side.
  const voice = el("div", { class: "holo-voice", hidden: true });
  const guests = new Map();
  screen.node.append(voice);
  const frame = svg("svg", { viewBox: `0 0 ${ROOM.width} ${ROOM.height}`, role: "group", "aria-label": t("会议室") },
    svg("defs", {},
      svg("radialGradient", { id: `${room}-spot` },
        svg("stop", { offset: "0%", class: "spot-core" }), svg("stop", { offset: "100%", class: "spot-edge" })),
      svg("linearGradient", { id: `${room}-table`, x1: 0, y1: 0, x2: 0, y2: 1 },
        svg("stop", { offset: "0%", class: "table-top" }), svg("stop", { offset: "100%", class: "table-bottom" }))),
    wall.node,
    svg("ellipse", { class: "floor", cx: ROOM.cx, cy: ROOM.cy, rx: ROOM.rx + 62, ry: ROOM.ry + 52 }),
    svg("ellipse", { class: "floor-ring", cx: ROOM.cx, cy: ROOM.cy, rx: ROOM.rx + 6, ry: ROOM.ry + 4 }),
    spot,
    svg("ellipse", { class: "table-shadow", cx: ROOM.cx, cy: ROOM.cy + 10, rx: TABLE.rx + 4, ry: TABLE.ry + 4 }),
    svg("ellipse", { class: "table", cx: ROOM.cx, cy: ROOM.cy, rx: TABLE.rx, ry: TABLE.ry, fill: `url(#${room}-table)` }),
    svg("ellipse", { class: "rim", cx: ROOM.cx, cy: ROOM.cy, rx: RIM.rx, ry: RIM.ry }),
    flow);
  const figures = new Map();
  roster
    .map((persona) => ({ persona, seat: places[persona.seat] }))
    .sort((a, b) => a.seat.y - b.seat.y)                 // those at the back first, so the front row overlaps them
    .forEach(({ persona, seat }) => {
      const figure = drawFigure(persona, seat);
      figure.addEventListener("click", () => pick(persona.id));
      figure.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          pick(persona.id);
        }
      });
      figures.set(persona.id, figure);
      frame.append(figure);
    });
  frame.append(lead);

  // A sign over each figure's head, for what it has concluded so far. Empty until it has said something.
  const signs = new Map(roster.map((persona) => {
    const seat = places[persona.seat];
    const sign = el("div", {
      class: "sign", hidden: true,
      style: `left:${(seat.x / ROOM.width) * 100}%;top:${((seat.y - 110 * seat.scale) / ROOM.height) * 100}%`,
    });
    return [persona.id, { node: sign, text: null }];
  }));

  const banner = el("div", { class: "stage-banner" });
  const rail = stageRail({ onSeek: (line) => show(line, true) });
  const brief = meetingBrief({ onSeek: (line) => show(line, true) });
  const net = talkNetwork({ roster, onPick: (id) => pick(id), onSeek: (line) => show(line, true) });
  const status = statusStrip();
  const team = teamBoard({ roster, onPick: (id) => pick(id) });
  const debate = debateBoard({ onSeek: (line) => show(line, true) });
  const board = el("div", { class: "board", hidden: true });
  const speech = el("div", { class: "speech", "aria-live": "polite", title: t("点一下，立刻显示整句") });
  const timeline = el("div", { class: "timeline", "aria-hidden": "true" });
  const scrub = el("input", { type: "range", min: 0, max: 0, value: 0, step: 1, "aria-label": t("发言进度") });
  const count = el("span", { class: "count" });
  const button = (label, title, action) => el("button", { type: "button", title, "aria-label": title, onclick: action, text: label });
  const play = el("button", { type: "button", class: "play", onclick: () => (state.playing ? pause() : start()) });
  const follow = el("button", {
    type: "button", "aria-pressed": "false", text: t("跟随最新"), title: t("有新发言时自动跳到最新一句"), onclick: () => setFollow(!state.follow),
  });
  const speed = el("select", { "aria-label": t("播放速度") },
    SPEEDS.map((value) => el("option", { value, selected: value === 1, text: `${value}×` })));
  speed.addEventListener("change", () => { state.speed = Number(speed.value); });
  scrub.addEventListener("input", () => show(Number(scrub.value) - 1, true));
  const controls = el("div", { class: "controls" },
    button(t("第一句"), t("回到第一句"), () => show(0, true)),
    button(t("上一句"), t("上一句"), () => show(state.index - 1, true)),
    play,
    button(t("下一句"), t("下一句"), () => show(state.index + 1, true)),
    button(t("最后一句"), t("跳到最后一句"), () => show(state.events.length - 1, true)),
    scrub, count, speed, follow,
    el("span", { class: "keys" }, el("kbd", { text: "←" }), " ", el("kbd", { text: "→" }), t(" 换句 · "), el("kbd", { text: t("空格") }), t(" 播放或暂停")));

  const trail = decisionTrail({ onSeek: (line) => show(line, true) });
  const heading = el("div", { class: "row spread" });
  const record = el("section", { class: "card record" });
  const person = el("section", { class: "card person" });
  const log = el("div", { class: "log" });
  const logHead = el("div", { class: "row spread" });
  // The market screen stands in the middle of the table; the words are said on the line under the room.
  const view = el("div", { class: "stage-view" },
    frame, el("div", { class: "signs" }, [...signs.values()].map((sign) => sign.node)), screen.node, links);
  const node = el("div", { class: "stack" },
    heading,
    brief.node,
    el("div", { class: "room" },
      el("div", { class: "stack" },
        el("div", { class: "stage" },
          el("div", { class: "stage-top" }, rail.node, el("div", { class: "stage-now" }, banner, board)),
          status.node, view, speech, timeline, controls),
        debate.node,
        trail.node,
        record),
      el("div", { class: "side" }, person, team.node, net.node, el("section", { class: "card" }, logHead, log))));

  // ── What is on show ────────────────────────────────────────────────────────

  const current = () => playback.current(state);
  const listed = () => state.events.filter((event) => !state.only || !state.picked || event.speaker === state.picked);
  const runKey = () => `${state.meeting && state.meeting.run ? state.meeting.run.run_id : ""}:${state.events.length}`;
  let typed = { key: null, finish: () => {} };          // which line is being shown letter by letter
  let drawn = { timeline: null, banner: null };          // what was last built, so nothing is rebuilt for nothing
  let trailed = { key: null, rows: new Map() };          // the decision trail's rows by ticker, for the market screen
  let linked = { key: null, docked: null };              // the line last thrown across the room, and whose consoles are docked

  /** Each ticker's line of the decision trail, worked out again only when the record has grown. */
  function rowsNow() {
    const key = runKey();
    if (key !== trailed.key) trailed = { key, rows: new Map(buildTrail(state.events).map((row) => [row.ticker, row])) };
    return trailed.rows;
  }

  /** The record is over at the line being played: the last line of a run that is no longer going. */
  const ended = () => !playback.isLive(state) && state.events.length > 0 && state.index >= state.events.length - 1;

  function paintScreen() {
    screen.show({ event: current(), rows: rowsNow(), events: state.events, index: state.index, ended: ended() });
  }

  function paintHeading() {
    clear(heading);
    const meeting = state.meeting;
    if (!meeting || !meeting.run) {
      heading.append(el("h2", { text: t("会议室") }), runState(null));
      return;
    }
    const run = meeting.run;
    heading.append(
      el("div", { class: "row" },
        el("h2", { text: `${meeting.session} ${word("meeting", run.kind || "close")}` }),
        runState(run),
        run.live ? el("span", { class: "chip", text: t("实时：新发言会自己出现") }) : el("span", { class: "chip chip-quiet", text: t("回放") }),
        meeting.stands === false ? el("span", { class: "chip", text: t("这是当天的另一次尝试，不是算数的那一次") }) : null),
      el("div", { class: "small muted" },
        t`开始于 ${when(run.started_at)}`, run.seconds === null ? "" : t`，用时 ${span(run.seconds)}`,
        t`，共 ${state.events.length} 条记录`,
        // The desk's meetings have a cut-off: nothing the ledger first saw after it was shown to anyone in them.
        run.as_of ? t`。信息截止 ${when(run.as_of)}，之后才取得的进不了这场会` : ""));
    if (run.error) heading.append(el("p", { class: "small said", text: t`这次运行记录的错误：${run.error}` }));
    if (run.status === "interrupted") {
      heading.append(el("p", { class: "small", text: t("账本里这次运行还写着“运行中”，但运行它的进程已经不在了，所以它不会再有新的发言。") }));
    }
  }

  /** Above the room: which stage the meeting is in, and the macro view once the pipeline has worked it out. */
  function paintTop(standing) {
    const event = current();
    const round = event && event.kind === "argument" && event.payload.round ? t`第 ${event.payload.round} 轮` : null;
    const key = event ? `${event.stage}|${event.ticker || ""}|${round || ""}` : "";
    if (key !== drawn.banner) {
      drawn.banner = key;
      if (event) {
        fill(banner,
          el("span", { class: "banner-stage", text: event.stage_title }),
          event.ticker ? el("span", { class: "banner-ticker", text: event.ticker }) : null,
          round ? el("span", { class: "chip chip-quiet", text: round }) : null);
        replay(banner, "sweep");
      } else {
        fill(banner, el("span", { class: "banner-stage muted", text: state.events.length ? t("还没开始") : t("没有记录") }));
      }
    }
    board.hidden = !standing.board;
    if (standing.board) {
      const { text, lean: way, reused } = standing.board;
      fill(board,
        el("span", { class: "small muted", text: t("宏观结论") }),
        el("strong", { class: way === "up" ? "up" : way === "down" ? "down" : null }, ARROW[way] ? `${ARROW[way]} ` : "", text),
        reused ? el("span", { class: "small muted", text: t`沿用 ${reused} 的` }) : null);
    }
  }

  function paintSpeech() {
    const event = current();
    const key = event ? `${runKey().split(":")[0]}:${event.seq}` : `none:${state.events.length}`;
    if (key === typed.key) return;                      // the same line as before: leave it as it stands
    typed.finish();
    clear(speech);
    speech.removeAttribute("style");
    if (!event) {
      typed = { key, finish: () => {} };
      speech.append(el("div", { class: "words muted", text: state.events.length
        ? t("按播放，从第一句开始看这一场。")
        : t("这一场还没有任何发言记录。流程运行起来之后，发言会按顺序出现在这里。") }));
      return;
    }
    const persona = byId.get(event.speaker);
    if (persona) speech.setAttribute("style", `--cloth: var(--tone-${persona.tone})`);
    const words = el("div", { class: "words", lang: "en" });
    speech.append(
      el("div", { class: "who" },
        persona ? portrait(persona) : el("span", { class: "portrait narrator", "aria-hidden": "true", text: "⚙" }),
        el("div", { class: "who-text" },
          el("div", { class: "who-name" }, persona ? el("span", { class: "badge" }, emblem(persona.emblem)) : null,
            el("strong", { text: event.speaker_title }),
            el("span", { class: "to-arrow", "aria-hidden": "true" }, el("i", {}), el("i", {}), el("i", {})),
            byId.has(event.to) ? portrait(byId.get(event.to)) : null,
            el("span", { class: "to", text: t` 对${event.to_title}` })),
          el("div", { class: "who-chips" },
            el("span", { class: "chip chip-quiet", text: event.stage_title }),
            event.ticker ? el("span", { class: "chip", text: event.ticker }) : null,
            el("span", { class: event.is_llm ? "chip" : "chip chip-rule", text: event.is_llm ? t("模型原话") : t("规则记录") }),
            el("span", { class: "muted", text: clock(event.ts) })))),
      words);
    // Said at reading pace, and never slower than the playback moves on.
    const finish = typeOut(words, event.remark, Math.min(900, playback.wait(event, state.speed) * 0.5));
    typed = { key, finish };
    replay(speech, "arrive");
  }

  function paintRecord() {
    clear(record);
    const event = current();
    record.append(el("h2", { text: t("这一句的完整记录") }));
    if (!event) {
      record.append(el("p", { class: "sub", text: t("选中一句发言后，这里显示它当时记下的全部内容。") }));
      return;
    }
    record.append(el("p", { class: "sub", text: t("字段名是中文，内容是当时记下的原文，没有改写。") }));
    const kept = Object.keys(event.payload).filter((key) => !HIDDEN.has(key));
    record.append(kept.length ? fields(event.payload) : el("p", { class: "muted", text: t("这一句除了上面那句话，没有记下别的内容。") }));
  }

  /** The stream along the table's edge from the speaker to the one spoken to, or all the way round when it is everyone. */
  function paintFlow(speaking, listening, everyone) {
    clear(flow);
    if (!speaking) return;
    const from = seatOf(speaking), start = angleOf(from);
    const lead = `M${point(feet(from))} L${point(onRim(start))}`;
    const routes = [];
    if (listening) {
      const to = seatOf(listening);
      let sweep = angleOf(to) - start;
      while (sweep > Math.PI) sweep -= 2 * Math.PI;
      while (sweep < -Math.PI) sweep += 2 * Math.PI;
      routes.push(`${lead} ${along(start, sweep)} L${point(feet(to))}`);
    } else if (everyone) {
      routes.push(`${lead} ${along(start, Math.PI)}`, `M${point(onRim(start))} ${along(start, -Math.PI)}`);
    }
    for (const route of routes) {
      flow.append(svg("path", { class: "flow-glow", d: route }), svg("path", { class: "flow-line", d: route }));
      if (!calm()) {
        flow.append(svg("circle", { class: "flow-dot", r: 4.5 },
          svg("animateMotion", { dur: listening ? "1.3s" : "2.4s", repeatCount: "indefinite", path: route })));
      }
    }
  }

  /**
   * The speakers this record has that have no figure, and whether each is a model or code. While one of them
   * is speaking, or being spoken to, the screen says so on its upper edge.
   */
  function paintVoice() {
    const key = runKey();
    if (key !== linked.docked) {
      linked.docked = key;
      guests.clear();
      for (const event of state.events) {
        for (const [id, title, model] of [[event.speaker, event.speaker_title, event.is_llm], [event.to, event.to_title, null]]) {
          if (!id || id === EVERYONE || byId.has(id)) continue;
          const guest = guests.get(id) || { title: title || id, model: null };
          if (model !== null && guest.model === null) guest.model = Boolean(model);
          guests.set(id, guest);
        }
      }
    }
    const event = current();
    const speaking = event && guests.has(event.speaker) ? guests.get(event.speaker) : null;
    const hearing = !speaking && event && guests.has(event.to) ? guests.get(event.to) : null;
    const guest = speaking || hearing;
    voice.hidden = !guest;
    screen.node.classList.toggle("voicing", Boolean(speaking));
    if (!guest) return;
    voice.classList.toggle("speaking", Boolean(speaking));
    fill(voice, el("i", { class: "voice-led", "aria-hidden": "true" }), el("b", { text: guest.title }),
      el("small", { text: guest.model ? t("模型") : t("代码") }), el("span", { text: speaking ? t("发言中") : t("在听") }));
  }

  /**
   * Where a speaker or listener is in the room: a figure's chest; for one with no figure, the point on the
   * screen's edge nearest to `toward` (the other end of the line), or the middle of its upper edge.
   */
  function anchorOf(id, toward = null) {
    if (figures.has(id)) return chest(seatOf(id));
    if (!guests.has(id)) return null;
    if (!toward) return [ROOM.cx, SCREEN.top];
    const x = Math.min(SCREEN.right, Math.max(SCREEN.left, toward[0]));
    const y = Math.min(SCREEN.bottom, Math.max(SCREEN.top, toward[1]));
    // Inside the screen's own box (nobody stands there), the nearest edge; outside it, the clamped point is on an edge already.
    if (x === toward[0] && y === toward[1]) return [x, SCREEN.top];
    return [x, y];
  }

  /** Send a few lights along a path once, the first ahead of the rest, and take them away when they arrive. */
  function fly(path, duration = 760) {
    const total = path.getTotalLength();
    const lights = [0, 1, 2].map((at) => svg("circle", { class: "link-comet", r: 5.5 - at * 1.4, style: `opacity:${1 - at * 0.28}` }));
    links.append(...lights);
    const began = window.performance.now();
    const step = (now) => {
      if (!lights[0].isConnected) return;                 // the meeting has moved on to another line
      const done = Math.min(1, (now - began) / duration);
      const eased = 1 - (1 - done) ** 3;
      lights.forEach((light, at) => {
        const spot = path.getPointAtLength(Math.max(0, eased - at * 0.045) * total);
        light.setAttribute("cx", spot.x.toFixed(1));
        light.setAttribute("cy", spot.y.toFixed(1));
      });
      if (done < 1) window.requestAnimationFrame(step);
      else lights.forEach((light) => light.remove());
    };
    window.requestAnimationFrame(step);
  }

  /**
   * The line being played, shown as what it is between two seats: thrown across the room from the speaker to
   * the one spoken to, or, said to everyone, a wave that each figure answers in turn, the nearest first.
   * Drawn once per line: stepping back onto the same line does not throw it again.
   */
  function paintLinks() {
    const event = current();
    const key = event ? `${runKey().split(":")[0]}:${event.seq}` : `none:${state.events.length}`;
    if (key === linked.key) return;
    linked.key = key;
    clear(links);
    for (const figure of figures.values()) {
      figure.classList.remove("receiving");
      figure.classList.remove("hears");
    }
    if (!event) return;
    const other = event.to && event.to !== EVERYONE && figures.has(event.to) ? chest(seatOf(event.to)) : null;
    const from = anchorOf(event.speaker, other);
    if (!from) return;
    links.setAttribute("style", `--tone: ${byId.has(event.speaker) ? `var(--tone-${byId.get(event.speaker).tone})` : "var(--accent)"}`);
    if (event.to === EVERYONE) {
      replay(lead, "hears");                              // said to everyone, the lead included
      // From a figure the wave runs out along the floor; from the screen, out of its middle.
      const ground = figures.has(event.speaker) ? feet(seatOf(event.speaker)) : [ROOM.cx, (SCREEN.top + SCREEN.bottom) / 2];
      links.append(svg("ellipse", { class: "link-wave", cx: ground[0], cy: ground[1], rx: 46, ry: 16 }),
        svg("ellipse", { class: "link-wave late", cx: ground[0], cy: ground[1], rx: 46, ry: 16 }));
      [...figures].filter(([id]) => id !== event.speaker)
        .map(([id, figure]) => ({ figure, far: Math.hypot(seatOf(id).x - from[0], seatOf(id).y - from[1]) }))
        .sort((a, b) => a.far - b.far)
        .forEach(({ figure }, at) => {
          figure.style.setProperty("--wave", String(at));
          replay(figure, "hears");
        });
      return;
    }
    // Two speakers with no figure both speak through the screen: there is nowhere for a line between them to go.
    if (!figures.has(event.speaker) && !figures.has(event.to)) return;
    const to = anchorOf(event.to, from);
    if (!to || event.to === event.speaker) return;
    const route = throwPath(from, to);
    const beam = svg("path", { class: "link-beam", d: route, pathLength: 1 });
    links.append(svg("path", { class: "link-trace", d: route }), beam, svg("circle", { class: "link-hit", cx: to[0], cy: to[1], r: 10 }));
    if (figures.has(event.to)) replay(figures.get(event.to), "receiving");
    if (!calm() && typeof beam.getTotalLength === "function") fly(beam);
  }

  function paintFigures(standing) {
    const event = current();
    const speaking = event && figures.has(event.speaker) ? event.speaker : null;
    const listening = event && figures.has(event.to) && event.to !== speaking ? event.to : null;
    for (const [id, figure] of figures) {
      figure.classList.toggle("speaking", id === speaking);
      figure.classList.toggle("listening", id === listening);
      figure.classList.toggle("dim", speaking !== null && id !== speaking && id !== listening);
      figure.classList.toggle("picked", id === state.picked);
      const persona = byId.get(id);
      const kind = persona.kind === "rule" ? t("规则") : t("模型");
      figure.querySelector(".role").textContent = id === speaking ? t("发言中") : id === listening ? t("在听") : `${kind} · ${persona.group}`;
      // A listener leans towards whoever is speaking.
      const lean = id === listening && speaking ? Math.sign(seatOf(speaking).x - seatOf(id).x) * 3 : 0;
      figure.style.setProperty("--lean", `${lean}deg`);
    }
    // The light rests on the speaker, or on the table when the pipeline itself is speaking.
    const lit = speaking ? seatOf(speaking) : null;
    spot.style.transform = lit ? `translate(${lit.x}px, ${lit.y - 46 * lit.scale}px)` : `translate(${ROOM.cx}px, ${ROOM.cy}px)`;
    spot.classList.toggle("off", !event);
    frame.classList.toggle("narrated", Boolean(event) && !speaking);
    paintFlow(speaking, listening, Boolean(event) && event.to === EVERYONE);
    paintVoice();
    paintLinks();

    for (const [id, sign] of signs) {
      const now = standing.signs[id] || null;
      const text = now ? now.text : null;
      sign.node.hidden = !now;
      if (text === sign.text) continue;
      sign.text = text;
      if (!now) continue;
      fill(sign.node,
        ARROW[now.lean] ? el("i", { class: now.lean, "aria-hidden": "true", text: ARROW[now.lean] }) : null, now.text);
      replay(sign.node, "pop");
    }
  }

  /** The whole record as a strip: one tick per line, grouped by stage and, in a debate, by ticker. */
  function paintTimeline() {
    const key = runKey();
    if (key !== drawn.timeline) {
      drawn.timeline = key;
      clear(timeline);
      for (const part of stretches(state.events)) {
        const ticks = el("div", { class: "ticks" });
        for (let at = part.from; at <= part.to; at += 1) {
          const event = state.events[at];
          const tick = el("button", {
            type: "button", class: event.is_llm ? "tick" : "tick tick-rule", tabindex: -1, "data-at": at, onclick: () => show(at, true),
          });
          tick.addEventListener("pointermove", (pointer) => showTip(pointer.clientX, pointer.clientY,
            t`第 ${at + 1} 句 · ${event.speaker_title} · ${event.stage_title}${event.ticker ? ` · ${event.ticker}` : ""}`,
            [{ value: event.remark, name: "", plain: true }]));
          tick.addEventListener("pointerleave", hideTip);
          ticks.append(tick);
        }
        const name = part.ticker ? `${part.title} ${part.ticker}` : part.title;
        timeline.append(el("div", { class: "stretch", style: `flex-grow:${part.to - part.from + 1}` },
          el("button", { type: "button", class: "stretch-name", tabindex: -1, title: name, onclick: () => show(part.from, true) }, name),
          ticks));
      }
    }
    for (const tick of timeline.querySelectorAll(".tick")) {
      const at = Number(tick.dataset.at);
      tick.classList.toggle("past", at < state.index);
      tick.classList.toggle("now", at === state.index);
    }
    for (const part of timeline.querySelectorAll(".stretch")) {
      part.classList.toggle("now", Boolean(part.querySelector(".tick.now")));
    }
  }

  function paintLog() {
    clear(logHead);
    clear(log);
    const persona = state.picked ? byId.get(state.picked) : null;
    logHead.append(el("h2", { text: state.only && persona ? t`${persona.title}的发言` : t("发言记录") }));
    if (persona) {
      logHead.append(el("button", {
        type: "button", class: "chip", "aria-pressed": String(state.only), text: state.only ? t("显示所有人") : t("只看这个人"),
        onclick: () => { state.only = !state.only; paintLog(); },
      }));
    }
    const rows = listed();
    if (rows.length === 0) {
      log.append(el("p", { class: "empty", text: state.only && persona ? t("这一场里这个人没有发言。") : t("还没有记录。") }));
      return;
    }
    const now = current();
    let lit = null;
    for (const event of rows) {
      const at = state.events.indexOf(event);
      const row = el("button", { type: "button", "aria-current": now === event ? "true" : null, onclick: () => show(at, true) },
        el("span", { class: "n", text: String(at + 1) }),
        el("span", {},
          el("span", { class: "t" }, el("span", { class: "at", text: clock(event.ts) }), event.speaker_title,
            el("span", { class: "to", text: ` → ${event.to_title}` }), ` · ${event.stage_title}${event.ticker ? ` · ${event.ticker}` : ""}`),
          el("span", { class: "w", lang: "en", text: event.remark })));
      log.append(row);
      if (now === event) lit = row;
    }
    // Keep the current line in view inside the list, without moving the page itself.
    if (lit) log.scrollTop = Math.max(0, lit.offsetTop - log.clientHeight / 2 + lit.offsetHeight / 2);
  }

  /** A recorded confidence as ten lamps and its number. */
  function gauge(value) {
    if (typeof value !== "number") return null;
    const lit = Math.round(Math.min(1, Math.max(0, value)) * 10);
    return el("span", { class: "gauge", role: "img", "aria-label": t`把握度 ${num(value, 2)}` },
      Array.from({ length: 10 }, (_, at) => el("i", { class: at < lit ? "on" : null })), el("b", { text: num(value, 2) }));
  }

  /** What a figure has concluded by the line being played: the last thing it said, in its recorded fields and own words. */
  function conclusion(persona) {
    const said = lastSaid(state.events, state.index, persona.id);
    const sign = standings(state.events, state.index).signs[persona.id];
    if (!said || !sign) return el("span", { class: "muted", text: t("到这一句为止，还没有发言。") });
    const record = said.payload || {};
    const quote = (label, text) => (text
      ? el("div", { class: "quote" }, el("small", { text: t`${label}（原话）` }), el("span", { class: "said", lang: "en", text })) : null);
    const found = [el("div", { class: "verdict" },
      el("strong", { class: sign.lean === "flat" ? null : sign.lean, text: sign.text }), gauge(record.confidence))];
    if (said.kind === "assessment") {
      found.push(quote(t("主要风险"), record.key_risk),
        (record.sector_tilts || []).length ? el("div", { class: "crafts" }, record.sector_tilts.map((call) =>
          el("span", { class: "craft", text: `${call.sector} ${word("tilt", call.tilt)}` }))) : null);
    } else if (said.kind === "view") {
      found.push(
        el("div", { class: "small muted", text: [
          typeof record.horizon_days === "number" ? t`期限 ${record.horizon_days} 个交易日` : null,
          typeof record.proposed_stop === "number" ? t`建议止损 ${num(record.proposed_stop, 2)}` : null,
        ].filter(Boolean).join(" · ") }),
        quote(t("什么情况下作废"), record.invalidation),
        (record.evidence || []).length ? el("div", { class: "cites" },
          el("small", { text: t("引用的数据（流程拿行情快照核对过，允许约 2% 的出入）") }),
          record.evidence.map((cited) => el("span", { class: cited.matches ? "cite ok" : "cite bad",
            text: `${cited.matches ? "✓" : "✗"} ${citationText(cited)}` }))) : null);
    } else if (said.kind === "argument") {
      found.push(quote(t("最有力的依据"), record.strongest_evidence));
    } else if (said.kind === "ruling") {
      found.push(quote(t("裁决理由"), record.reasoning), quote(t("什么情况下反转"), record.invalidation));
    } else if (said.kind === "proposals" || said.kind === "checkin_decisions") {
      found.push(quote(t("整体思路"), record.summary));
    } else if (said.kind === "view_update") {
      const cited = (record.cited || []).length;
      found.push(
        el("div", { class: "small muted", text: t`依据了 ${cited} 条新消息` }),
        quote(t("什么变了"), record.reason),
        (record.data_warnings || []).length ? el("div", { class: "cites" },
          el("small", { text: t("流程核对时标出的问题") }),
          record.data_warnings.map((warning) => el("span", { class: "cite bad", lang: "en", text: `✗ ${warning}` }))) : null);
    }
    return found;
  }

  function paintPerson() {
    clear(person);
    // Whoever was picked; with nobody picked, whoever is speaking.
    const speaking = current() ? byId.get(current().speaker) || null : null;
    const persona = state.picked ? byId.get(state.picked) : speaking;
    if (!persona) {
      person.removeAttribute("style");
      person.append(
        el("h2", { text: t("这些人是谁") }),
        el("p", { text: t("十四个角色：十二个是语言模型，风控官和交易员是写死的规则，不是模型。播放时这里显示正在发言的人；点一个人，固定看他。") }));
      return;
    }
    // Counted over the lines played so far. The whole session's tally is the server's, of every line of
    // the run: it is shown only once the record is over.
    const said = state.events.slice(0, state.index + 1).filter((event) => event.speaker === persona.id).length;
    const today = state.meeting && state.meeting.today ? state.meeting.today[persona.id] : null;
    const records = (state.meeting && state.meeting.records) || { as_of: null, lines: {} };
    const past = records.as_of
      ? records.lines[persona.id] || t("评估没有单独衡量这个角色，所以没有成绩。")
      : t("还没有评估报告。");
    const field = state.market && state.market.available
      ? readings(persona.reads.filter((id) => id !== "sectors"), state.market) : [];
    person.setAttribute("style", `--cloth: var(--tone-${persona.tone})`);
    fill(person,
      el("h2", {}, portrait(persona), el("span", { class: "badge" }, emblem(persona.emblem)), persona.title,
        el("span", { class: persona.kind === "rule" ? "chip chip-rule" : "chip", text: persona.kind === "rule" ? t("规则") : t("模型") }),
        el("span", { class: "chip chip-quiet", text: persona.group })),
      el("p", { text: persona.does }),
      el("div", { class: "crafts" }, persona.craft.map((skill) => el("span", { class: "craft", text: skill }))),
      el("div", { class: "fact" }, el("b", { text: t("到这一句为止的结论") }), conclusion(persona)),
      field.length ? el("div", { class: "fact" },
        el("b", { text: t`这个领域的数据（${state.market.session} 的快照）` }),
        el("div", { class: "field" }, field.map((item) => el("div", { class: "field-row" },
          el("span", { text: item.name }), el("b", { text: item.value }),
          el("i", { class: item.moves[0] && item.moves[0].lean !== "flat" ? item.moves[0].lean : null,
            text: item.moves[0] ? `${item.moves[0].label} ${item.moves[0].text}` : "" }))))) : null,
      el("div", { class: "fact" }, el("b", { text: t("到这一句为止的发言次数") }), t`${said} 次`),
      ended() ? el("div", { class: "fact" }, el("b", { text: t("整场合计") }), today || t("这一场里没有发言。")) : null,
      el("div", { class: "fact" },
        el("b", { text: records.as_of ? t`历史成绩（${records.as_of} 的评估，按到那天为止的全部记录算）` : t("历史成绩") }), past),
      el("div", { class: "row" }, state.picked
        ? el("button", { type: "button", class: "chip", text: t("取消选中"), onclick: () => pick(null) })
        : el("span", { class: "small muted", text: t("现在显示的是正在发言的人。点任何一个人，固定看他。") })));
  }

  function paintProgress() {
    scrub.max = String(state.events.length);
    scrub.value = String(state.index + 1);
    count.textContent = `${state.index + 1} / ${state.events.length}`;
    play.textContent = state.playing ? t("⏸ 暂停") : t("▶ 播放");
    play.title = state.playing ? t("暂停") : t("播放");
    play.setAttribute("aria-label", play.title);
    play.classList.toggle("playing", state.playing);
    follow.setAttribute("aria-pressed", String(state.follow));
    follow.hidden = !playback.isLive(state);
  }

  function paint() {
    const standing = standings(state.events, state.index);
    paintHeading();
    paintTop(standing);
    paintSpeech();
    paintRecord();
    paintFigures(standing);
    paintScreen();
    paintTimeline();
    const run = state.meeting && state.meeting.run ? state.meeting.run.run_id : "";
    brief.load(state.events, { run, live: playback.isLive(state) });
    rail.update(state.events, state.index, run);
    net.update(state.events, state.index);
    const about = current() && current().ticker ? current().ticker : null;
    wall.focus(about);
    status.update({
      event: current(), events: state.events, index: state.index, run: state.meeting ? state.meeting.run : null,
      market: state.market, focused: about,
    });
    team.update(state.events, state.index, state.picked);
    debate.update(state.events, state.index, ended());
    trail.load(state.events, run, playback.isLive(state));
    trail.update(state.index);
    paintLog();
    paintPerson();
    paintProgress();
  }

  // ── Moving through the record ──────────────────────────────────────────────
  // The rules are in playback.js. Here they are given a clock and a repaint.

  function halt() {
    window.clearTimeout(state.timer);
    state.timer = null;
  }

  function show(index, byHand = false) {
    playback.show(state, index, byHand);
    if (byHand) halt();
    paint();
  }

  function tick() {
    state.timer = null;
    const outcome = playback.advance(state);
    if (outcome === "shown") {
      paint();
      state.timer = window.setTimeout(tick, playback.wait(current(), state.speed));
    } else {
      paintProgress();            // ended, or waiting for a run that is still going to say its next line
    }
  }

  function start() {
    halt();
    if (playback.start(state)) tick();
  }

  function pause() {
    playback.pause(state);
    halt();
    paintProgress();
  }

  function setFollow(on) {
    playback.setFollow(state, on);
    if (on) halt();
    paint();
  }

  function pick(id) {
    state.picked = state.picked === id ? null : id;
    if (!state.picked) state.only = false;
    for (const [figure, drawing] of figures) drawing.classList.toggle("picked", figure === state.picked);
    team.update(state.events, state.index, state.picked);
    paintLog();
    paintPerson();
  }

  speech.addEventListener("click", () => typed.finish());

  // ── What the page hands over ───────────────────────────────────────────────

  /** Put a session's record in the room. Keeps the reader's place when it is the same run as before. */
  function load(meeting) {
    if (!playback.load(state, meeting)) halt();
    paint();
  }

  /** Add events recorded since the last look. Only those of the run in the room, and each only once. */
  function extend(runId, events) {
    if (playback.extend(state, runId, events) === 0) return false;
    paint();
    if (state.playing && state.timer === null) tick();          // it was waiting at the end for this line
    return true;
  }

  // The keyboard moves through the record, as on any player: left and right a line at a time, the space bar to play.
  // Not while a control has the keys (a button, the speed, the slider), and not a key something else has taken.
  function onKey(event) {
    const tag = event.target && event.target.tagName ? String(event.target.tagName).toLowerCase() : "";
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || ["input", "select", "textarea", "button"].includes(tag)) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      show(state.index + (event.key === "ArrowRight" ? 1 : -1), true);
    } else if (event.key === " ") {
      event.preventDefault();
      if (state.playing) pause();
      else start();
    }
  }
  const keyed = typeof document.addEventListener === "function";
  if (keyed) document.addEventListener("keydown", onKey);

  function destroy() {
    window.clearTimeout(state.timer);
    typed.finish();
    hideTip();
    if (keyed) document.removeEventListener("keydown", onKey);
  }

  /** Take the market data of the run in the room (as /api/market answers): for the screen and the figures' panels. */
  function setMarket(market) {
    state.market = market;
    screen.load(market);
    wall.load(market);
    const about = current() && current().ticker ? current().ticker : null;
    wall.focus(about);
    status.update({
      event: current(), events: state.events, index: state.index, run: state.meeting ? state.meeting.run : null, market, focused: about,
    });
    paintPerson();
  }

  paint();
  return {
    node, load, extend, destroy, setMarket, seek: (line) => show(line, true),
    runId: () => (state.meeting && state.meeting.run ? state.meeting.run.run_id : null),
    status: () => (state.meeting && state.meeting.run ? state.meeting.run.status : null),
  };
}
