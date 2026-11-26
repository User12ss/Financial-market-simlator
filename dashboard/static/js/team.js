// The team as its lead sees it, beside the room: every seat in its group, what each has concluded by the line
// being played, and how often it has spoken; and, under the room, the debate over the ticker in hand, round by
// round, from the first argument to the ruling and what became of it. Read off the record, in its own words.

import { el, clear, word, num, showTip, hideTip } from "./dom.js";
import { buildTrail, outcome, outcomeText, standings } from "./decisions.js";
import { exchanges } from "./links.js";
import { t } from "./i18n.js";

const ARROW = { up: "▲", down: "▼", flat: "" };
const GROUPS = [t("决策"), t("标的组"), t("宏观组"), t("辩论"), t("执行")];

/**
 * The seats as a table. `update(events, index)` shows where each stands by that line; `onPick(id)` is called
 * when a row is chosen.
 */
export function teamBoard({ roster, onPick }) {
  const groups = [...new Set([...GROUPS, ...roster.map((persona) => persona.group)])]
    .map((name) => ({ name, seats: roster.filter((persona) => persona.group === name) })).filter((group) => group.seats.length);
  const rows = new Map();
  const body = el("div", { class: "team-body", role: "table", "aria-label": t("团队的席位") });
  for (const group of groups) {
    body.append(el("div", { class: "team-group", role: "row" }, el("span", { text: group.name }), el("small", { text: t`${group.seats.length} 席` })));
    for (const persona of group.seats) {
      const stand = el("span", { class: "team-stand" });
      const said = el("span", { class: "team-said" });
      const row = el("button", {
        type: "button", class: "team-row", role: "row", style: `--tone: var(--tone-${persona.tone})`, onclick: () => onPick(persona.id),
      },
        el("i", { class: persona.kind === "rule" ? "team-mark rule" : "team-mark", "aria-hidden": "true" }),
        el("span", { class: "team-name" }, persona.title, el("small", { text: persona.kind === "rule" ? t("规则") : t("模型") })),
        stand, said);
      rows.set(persona.id, { row, stand, said, text: null });
      body.append(row);
    }
  }
  const head = el("p", { class: "sub" });
  const node = el("section", { class: "card team" }, el("h2", { text: t("你的团队") }), head, body);

  function update(events, index, picked = null) {
    const event = index >= 0 && index < events.length ? events[index] : null;
    const { signs } = standings(events, index);
    const tally = exchanges(events, index);
    const spoken = [...rows.keys()].filter((id) => tally.said.get(id)).length;
    head.textContent = t`${rows.size} 个席位，到这一句为止 ${spoken} 个发过言。点一行，固定看这个席位。`;
    for (const [id, seat] of rows) {
      const sign = signs[id] || null;
      const text = sign ? `${ARROW[sign.lean] ? `${ARROW[sign.lean]} ` : ""}${sign.text}` : "—";
      if (text !== seat.text) {
        seat.text = text;
        seat.stand.textContent = text;
        seat.stand.dataset.lean = sign ? sign.lean : "none";
      }
      const count = tally.said.get(id) || 0;
      seat.said.textContent = count ? t`${count} 句` : "";
      seat.row.dataset.state = event && event.speaker === id ? "speaking" : event && event.to === id ? "listening" : count ? "spoken" : "silent";
      seat.row.setAttribute("aria-pressed", String(picked === id));
    }
  }

  return { node, update };
}

/** The ticker whose debate is in hand at line `index`: the one being argued or ruled on, else the latest one argued so far. */
export function debatedTicker(events, index) {
  for (let at = Math.min(index, events.length - 1); at >= 0; at -= 1) {
    if (events[at].kind === "argument") return events[at].ticker || null;
  }
  return null;
}

/**
 * One ticker's debate by line `index`: its rounds with each side's line, the judge's ruling once made, and how
 * many rounds there were. A side that has not spoken in a round yet has no line there.
 */
export function debateOf(events, index, ticker) {
  const rounds = new Map();
  let ruling = null;
  const upTo = Math.min(index, events.length - 1);
  for (let at = 0; at <= upTo; at += 1) {
    const event = events[at];
    if (event.ticker !== ticker) continue;
    const said = event.payload || {};
    if (event.kind === "argument") {
      const number = said.round || 1;
      const round = rounds.get(number) || { round: number, bull: null, bear: null };
      const side = event.speaker === "bear" || said.side === "bear" ? "bear" : "bull";
      round[side] = { index: at, remark: event.remark, evidence: said.strongest_evidence || "" };
      rounds.set(number, round);
    } else if (event.kind === "ruling") {
      ruling = { index: at, stance: said.stance, confidence: said.confidence, remark: event.remark };
    }
  }
  return { ticker, rounds: [...rounds.values()].sort((a, b) => a.round - b.round), ruling };
}

/**
 * The debate in hand as a board. `update(events, index, ended)` redraws it for the line being played;
 * `onSeek(line)` is called with the place of a line that was picked.
 */
export function debateBoard({ onSeek }) {
  const title = el("h2", { text: t("多轮辩论") });
  const lead = el("p", { class: "sub" });
  const body = el("div", { class: "debate-body" });
  const node = el("section", { class: "card debate" }, title, lead, body);
  let drawn = null;

  function line(side, entry, index) {
    if (!entry) return el("div", { class: `debate-cell ${side} none`, text: t("还没有发言") });
    const cell = el("button", {
      type: "button", class: `debate-cell ${side}${entry.index === index ? " now" : ""}`, title: t("跳到这一句原话"), onclick: () => onSeek(entry.index),
    }, el("span", { class: "debate-words", lang: "en", text: entry.remark }),
      entry.evidence ? el("span", { class: "debate-proof" }, el("small", { text: t("最有力的依据（原话）") }), el("span", { lang: "en", text: entry.evidence })) : null);
    cell.addEventListener("pointermove", (pointer) => showTip(pointer.clientX, pointer.clientY, t`第 ${entry.index + 1} 句`, [{ value: entry.remark, name: "", plain: true }]));
    cell.addEventListener("pointerleave", hideTip);
    return cell;
  }

  function update(events, index, ended = false) {
    const ticker = debatedTicker(events, index);
    const key = `${events.length}:${index}:${ended}`;
    if (key === drawn) return;
    drawn = key;
    clear(body);
    hideTip();
    if (!ticker) {
      title.textContent = t("多轮辩论");
      lead.textContent = events.some((event) => event.kind === "argument")
        ? t("到这一句为止，辩论还没有开始。") : t("这一场的记录里没有辩论。");
      return;
    }
    const debate = debateOf(events, index, ticker);
    const row = buildTrail(events).find((item) => item.ticker === ticker);
    title.textContent = t`多轮辩论 · ${ticker}`;
    lead.textContent = t`多方和空方一轮一轮地交锋，裁判听完后裁定。到这一句为止 ${debate.rounds.length} 轮`
      + t`${debate.ruling ? t("，已裁定") : t("，还没有裁定")}。点任一格，回到那句原话。`;
    body.append(el("div", { class: "debate-head" }, el("span", {}), el("span", { class: "bull", text: t("▲ 多方：为买入或继续持有找理由") }),
      el("span", { class: "bear", text: t("▼ 空方：为不买或卖出找理由") })));
    for (const round of debate.rounds) {
      body.append(el("div", { class: "debate-round" }, el("span", { class: "debate-no", text: t`第 ${round.round} 轮` }),
        line("bull", round.bull, index), line("bear", round.bear, index)));
    }
    if (debate.ruling) {
      const lean = debate.ruling.stance === "own" ? "up" : "down";
      const verdict = el("button", {
        type: "button", class: `debate-ruling${debate.ruling.index === index ? " now" : ""}`, title: t("跳到裁判的原话"), onclick: () => onSeek(debate.ruling.index),
      },
        el("span", { class: "debate-no", text: t("裁判") }),
        el("strong", { class: lean, text: t`${ARROW[lean]} 裁定${word("ownership", debate.ruling.stance)}` }),
        typeof debate.ruling.confidence === "number" ? el("span", { class: "debate-sure", text: t`把握度 ${num(debate.ruling.confidence, 2)}` }) : null,
        el("span", { class: "debate-words", lang: "en", text: debate.ruling.remark }));
      body.append(verdict);
    }
    // What became of it afterwards, in the trail's own words, as far as the record has got by this line.
    if (row && debate.ruling) {
      const result = outcome(row, index);
      if (!["ruled", "debating", "viewed", "updated", "waiting"].includes(result.kind)) {
        body.append(el("div", { class: "debate-after" }, el("span", { class: "debate-no", text: t("之后") }), el("span", { text: outcomeText(result, ended) })));
      }
    }
  }

  return { node, update };
}
