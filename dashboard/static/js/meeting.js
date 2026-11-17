// The meeting room: fourteen figures in a circle and one session's record
// played back in the order it was recorded. Whoever is speaking is lit, the
// one spoken to is marked, and the words appear in the middle exactly as they
// were written down. The room never invents a line: with no record it is empty.

import { el, svg, clear, clock, span, when, word, runState, DASH } from "./dom.js";
import { drawFigure, seats } from "./figures.js";
import * as playback from "./playback.js";

const ROOM = { width: 1000, height: 680, cx: 500, cy: 352, rx: 400, ry: 232 };
const SPEEDS = [0.5, 1, 2, 4];

// Chinese names for the recorded fields. A field without one is shown under its recorded name.
const FIELDS = {
  ticker: "标的", direction: "方向", confidence: "把握度", horizon_days: "期限（交易日）", thesis: "论点",
  catalysts: "催化因素", evidence: "依据", invalidation: "什么情况下作废", held: "是否已持有", session_date: "交易日",
  data_warnings: "数据提醒", stance: "立场", summary: "摘要", key_risk: "主要风险", sector_tilts: "行业倾向",
  agent_name: "记录的角色名", side: "一方", round: "第几轮", argument: "论述", strongest_evidence: "最有力的依据",
  trigger: "为什么辩论", reasoning: "裁决理由", proposals: "提案", action: "动作", reason: "理由", source: "来源",
  status: "状态", note: "备注", request: "申请的订单", decision: "风控的决定", order_id: "订单号", kind: "订单类型",
  shares: "股数", price: "成交价", fill_date: "成交日", pnl_pct: "相对成本的盈亏（%）", limit_price: "限价",
  stop_price: "止损价", protective_stop: "成交后挂的止损", day_only: "仅当日有效", created_session: "下单的交易日",
  filled_price: "成交价", filled_date: "成交日", risk_appetite: "风险偏好", key_risks: "主要风险",
  assessments: "各分析师的评估", requested_shares: "申请股数", approved_shares: "批准股数", verdict: "结论",
  reasons: "原因", tilt: "倾向", sector: "行业", supporters: "支持者", proposed_stop: "分析师建议的止损",
  stop_reason: "止损理由",
};
const HIDDEN = new Set(["remark", "agent_id"]);          // the remark is already in the middle of the room

/** A recorded value, laid out for reading. Text is passed on untouched. */
function shown(value) {
  if (value === null || value === undefined || value === "") return el("span", { class: "muted", text: DASH });
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return el("span", { class: "muted", text: "无" });
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

export function meetingRoom({ roster }) {
  const byId = new Map(roster.map((persona) => [persona.id, persona]));
  const places = seats(roster.length, ROOM);
  const state = { ...playback.newPlayback(), picked: null, only: false, timer: null };

  // ── The room itself ────────────────────────────────────────────────────────
  const aims = svg("g", {});
  const frame = svg("svg", { viewBox: `0 0 ${ROOM.width} ${ROOM.height}`, role: "group", "aria-label": "会议室" },
    svg("ellipse", { class: "floor", cx: ROOM.cx, cy: ROOM.cy, rx: ROOM.rx + 62, ry: ROOM.ry + 52 }), aims);
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

  const speech = el("div", { class: "speech", "aria-live": "polite" });
  const steps = el("div", { class: "steps", "aria-label": "流程阶段" });
  const scrub = el("input", { type: "range", min: 0, max: 0, value: 0, step: 1, "aria-label": "发言进度" });
  const count = el("span", { class: "count" });
  const button = (label, title, action) => el("button", { type: "button", title, "aria-label": title, onclick: action, text: label });
  const play = button("▶ 播放", "播放", () => (state.playing ? pause() : start()));
  const follow = el("button", {
    type: "button", "aria-pressed": "false", text: "跟随最新", title: "有新发言时自动跳到最新一句", onclick: () => setFollow(!state.follow),
  });
  const speed = el("select", { "aria-label": "播放速度" },
    SPEEDS.map((value) => el("option", { value, selected: value === 1, text: `${value}×` })));
  speed.addEventListener("change", () => { state.speed = Number(speed.value); });
  scrub.addEventListener("input", () => show(Number(scrub.value) - 1, true));
  const controls = el("div", { class: "controls" },
    button("第一句", "回到第一句", () => show(0, true)),
    button("上一句", "上一句", () => show(state.index - 1, true)),
    play,
    button("下一句", "下一句", () => show(state.index + 1, true)),
    button("最后一句", "跳到最后一句", () => show(state.events.length - 1, true)),
    scrub, count, speed, follow);

  const heading = el("div", { class: "row spread" });
  const record = el("section", { class: "card record" });
  const person = el("section", { class: "card person" });
  const log = el("div", { class: "log" });
  const logHead = el("div", { class: "row spread" });
  const node = el("div", { class: "stack" },
    heading,
    el("div", { class: "room" },
      el("div", { class: "stack" },
        el("div", { class: "stage" }, el("div", { class: "stage-view" }, frame, speech), steps, controls),
        record),
      el("div", { class: "side" }, person, el("section", { class: "card" }, logHead, log))));

  // ── What is on show ────────────────────────────────────────────────────────

  const current = () => playback.current(state);
  const listed =() => state.events.filter((event) => !state.only || !state.picked || event.speaker === state.picked);

  function paintHeading() {
    clear(heading);
    const meeting = state.meeting;
    if (!meeting || !meeting.run) {
      heading.append(el("h2", { text: "会议室" }), runState(null));
      return;
    }
    const run = meeting.run;
    heading.append(
      el("div", { class: "row" },
        el("h2", { text: `${meeting.session} 这一场` }),
        runState(run),
        run.live ? el("span", { class: "chip", text: "实时：新发言会自己出现" }) : el("span", { class: "chip chip-quiet", text: "回放" }),
        meeting.stands === false ? el("span", { class: "chip", text: "这是当天的另一次尝试，不是算数的那一次" }) : null),
      el("div", { class: "small muted" },
        `开始于 ${when(run.started_at)}`, run.seconds === null ? "" : `，用时 ${span(run.seconds)}`,
        `，共 ${state.events.length} 条记录`));
    if (run.error) heading.append(el("p", { class: "small said", text: `这次运行记录的错误：${run.error}` }));
    if (run.status === "interrupted") {
      heading.append(el("p", { class: "small", text: "账本里这次运行还写着“运行中”，但运行它的进程已经不在了，所以它不会再有新的发言。" }));
    }
  }

  function paintSteps() {
    clear(steps);
    const order = [];
    for (const event of state.events) if (!order.includes(event.stage)) order.push(event.stage);
    const now = current();
    for (const stage of order) {
      const first = state.events.findIndex((event) => event.stage === stage);
      steps.append(el("button", {
        type: "button", class: "chip", "aria-current": now && now.stage === stage ? "step" : null,
        text: state.events[first].stage_title, onclick: () => show(first, true),
      }));
    }
  }

  function paintSpeech() {
    clear(speech);
    const event = current();
    if (!event) {
      speech.append(el("div", { class: "words muted", text: state.events.length
        ? "按播放，从第一句开始看这一场。"
        : "这一场还没有任何发言记录。流程运行起来之后，发言会按顺序出现在这里。" }));
      return;
    }
    speech.append(
      el("div", { class: "who" },
        el("strong", { text: event.speaker_title }),
        el("span", { text: `对${event.to_title}` }),
        el("span", { class: "chip chip-quiet", text: event.stage_title }),
        event.ticker ? el("span", { class: "chip", text: event.ticker }) : null,
        el("span", { class: event.is_llm ? "chip" : "chip chip-rule", text: event.is_llm ? "模型原话" : "规则记录" }),
        el("span", { class: "muted", text: clock(event.ts) })),
      el("div", { class: "words", lang: "en", text: event.remark }));
  }

  function paintRecord() {
    clear(record);
    const event = current();
    record.append(el("h2", { text: "这一句的完整记录" }));
    if (!event) {
      record.append(el("p", { class: "sub", text: "选中一句发言后，这里显示它当时记下的全部内容。" }));
      return;
    }
    record.append(el("p", { class: "sub", text: "字段名是中文，内容是当时记下的原文，没有改写。" }));
    const kept = Object.keys(event.payload).filter((key) => !HIDDEN.has(key));
    record.append(kept.length ? fields(event.payload) : el("p", { class: "muted", text: "这一句除了上面那句话，没有记下别的内容。" }));
  }

  function paintFigures() {
    const event = current();
    const speaking = event && figures.has(event.speaker) ? event.speaker : null;
    const listening = event && figures.has(event.to) && event.to !== speaking ? event.to : null;
    for (const [id, figure] of figures) {
      figure.classList.toggle("speaking", id === speaking);
      figure.classList.toggle("listening", id === listening);
      figure.classList.toggle("dim", speaking !== null && id !== speaking && id !== listening);
      figure.classList.toggle("picked", id === state.picked);
      const persona = byId.get(id);
      const kind = persona.kind === "rule" ? "规则" : "模型";
      figure.querySelector(".role").textContent = id === speaking ? "发言中" : id === listening ? "在听" : `${kind} · ${persona.group}`;
    }
    clear(aims);
    if (speaking && listening) {
      const from = places[byId.get(speaking).seat], to = places[byId.get(listening).seat];
      const chest = (seat) => [seat.x, seat.y - 60 * seat.scale];
      const [x0, y0] = chest(from), [x1, y1] = chest(to);
      const length = Math.hypot(x1 - x0, y1 - y0) || 1;
      const ux = (x1 - x0) / length, uy = (y1 - y0) / length;
      const sx = x0 + ux * 30, sy = y0 + uy * 30, ex = x1 - ux * 34, ey = y1 - uy * 34;
      aims.append(
        svg("path", { class: "aim", d: `M${sx.toFixed(1)},${sy.toFixed(1)} L${ex.toFixed(1)},${ey.toFixed(1)}` }),
        svg("path", { class: "aim-head", d: `M${ex + ux * 12},${ey + uy * 12} L${ex - uy * 6},${ey + ux * 6} L${ex + uy * 6},${ey - ux * 6} Z` }));
    }
  }

  function paintLog() {
    clear(logHead);
    clear(log);
    const persona = state.picked ? byId.get(state.picked) : null;
    logHead.append(el("h2", { text: state.only && persona ? `${persona.title}的发言` : "发言记录" }));
    if (persona) {
      logHead.append(el("button", {
        type: "button", class: "chip", "aria-pressed": String(state.only), text: state.only ? "显示所有人" : "只看这个人",
        onclick: () => { state.only = !state.only; paintLog(); },
      }));
    }
    const rows = listed();
    if (rows.length === 0) {
      log.append(el("p", { class: "empty", text: state.only && persona ? "这一场里这个人没有发言。" : "还没有记录。" }));
      return;
    }
    const now = current();
    let lit = null;
    for (const event of rows) {
      const at = state.events.indexOf(event);
      const row = el("button", { type: "button", "aria-current": now === event ? "true" : null, onclick: () => show(at, true) },
        el("span", { class: "n", text: String(at + 1) }),
        el("span", {},
          el("span", { class: "t", text: `${event.speaker_title} · ${event.stage_title}${event.ticker ? ` · ${event.ticker}` : ""}` }),
          el("span", { class: "w", lang: "en", text: event.remark })));
      log.append(row);
      if (now === event) lit = row;
    }
    // Keep the current line in view inside the list, without moving the page itself.
    if (lit) log.scrollTop = Math.max(0, lit.offsetTop - log.clientHeight / 2 + lit.offsetHeight / 2);
  }

  function paintPerson() {
    clear(person);
    const persona = state.picked ? byId.get(state.picked) : null;
    if (!persona) {
      person.append(
        el("h2", { text: "这些人是谁" }),
        el("p", { text: "十四个角色：十二个是语言模型，风控官和交易员是写死的规则，不是模型。点一个人，看他今天做了什么。" }));
      return;
    }
    const said = state.events.filter((event) => event.speaker === persona.id).length;
    const today = state.meeting && state.meeting.today ? state.meeting.today[persona.id] : null;
    const records = (state.meeting && state.meeting.records) || { as_of: null, lines: {} };
    const past = records.as_of
      ? records.lines[persona.id] || "评估没有单独衡量这个角色，所以没有成绩。"
      : "还没有评估报告。";
    person.append(
      el("h2", {}, persona.title,
        el("span", { class: persona.kind === "rule" ? "chip chip-rule" : "chip", text: persona.kind === "rule" ? "规则" : "模型" }),
        el("span", { class: "chip chip-quiet", text: persona.group })),
      el("p", { text: persona.does }),
      el("div", { class: "fact" }, el("b", { text: "这一场" }), today || "这一场里还没有发言。"),
      el("div", { class: "fact" }, el("b", { text: "发言次数" }), `${said} 次`),
      el("div", { class: "fact" },
        el("b", { text: records.as_of ? `到目前为止的成绩（${records.as_of} 的评估）` : "到目前为止的成绩" }), past),
      el("div", { class: "row" },
        el("button", { type: "button", class: "chip", text: "取消选中", onclick: () => pick(null) })));
  }

  function paintProgress() {
    scrub.max = String(state.events.length);
    scrub.value = String(state.index + 1);
    count.textContent = `${state.index + 1} / ${state.events.length}`;
    play.textContent = state.playing ? "⏸ 暂停" : "▶ 播放";
    play.title = state.playing ? "暂停" : "播放";
    play.setAttribute("aria-label", play.title);
    follow.setAttribute("aria-pressed", String(state.follow));
    follow.hidden = !playback.isLive(state);
  }

  function paint() {
    paintHeading();
    paintSteps();
    paintSpeech();
    paintRecord();
    paintFigures();
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
    paintFigures();
    paintLog();
    paintPerson();
  }

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

  function destroy() {
    window.clearTimeout(state.timer);
  }

  paint();
  return { node, load, extend, destroy, runId: () => (state.meeting && state.meeting.run ? state.meeting.run.run_id : null),
    status: () => (state.meeting && state.meeting.run ? state.meeting.run.status : null) };
}
