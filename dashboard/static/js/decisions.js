// How each decision of a session came about, worked out from that session's
// record and nothing else: for every ticker, the steps it went through (view,
// debate, ruling, proposal, check, risk, order) and where it stood at any
// point of the playback. Nothing here is written by a model or made up: every
// label is a recorded field, and every number is a recorded number or a count
// of recorded lines. What is said about a point of the playback uses only the
// lines up to that point. No page in sight, so it can be tested on its own.

import { word } from "./dom.js";

/** The steps a ticker can go through, in the order the pipeline takes them. */
export const STEPS = ["view", "debate", "ruling", "proposal", "check", "risk", "order"];
const MOVES = new Set(["buy", "sell", "trim", "withdraw"]);          // proposals that would change the book
const MACRO = new Set([
  "central_bank", "fiscal_policy", "equity_market", "fixed_income", "forex", "commodities_inflation", "sentiment",
]);

// What Python's str.strip() takes off the ends of a string. It is not quite what JavaScript's trim() takes off.
const SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const EDGES = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");

/** A ticker as the pipeline reads it out of a proposal (`ticker.strip().upper()` in pipeline/checks.py). */
export const tickerOf = (written) => String(written ?? "").replace(EDGES, "").toUpperCase();
const two = (value) => (typeof value === "number" ? value.toFixed(2) : "");

/**
 * Every ticker the session's record mentions, in order of first mention, with
 * the step each line belongs to. `index` is the line's place in the record.
 *
 * The pipeline counts only the first proposal for a ticker, so only the first
 * is kept here, with the check that was made of it; later ones are counted in
 * `repeats`. A holding left without an accepted decision is held by the
 * checks' own default: that line is `check.kept` when it follows a proposal
 * that was turned back, and the check itself when nothing was proposed.
 *
 * A check-in of the desk (it opens with the news desk's briefing) goes through
 * fewer steps, and they mean other things. Its "view" is the analyst's update
 * of a view that already stands; its "proposal" is the manager's decision to
 * keep, cut or take back; and its last step is an order placed, a waiting buy
 * taken back, or, during the session, a decision noted for the close meeting.
 * Such a row says so with `checkin`.
 */
export function buildTrail(events) {
  const rows = new Map();
  const checkin = events.some((event) => event.kind === "briefing");
  const of = (ticker) => {
    if (!rows.has(ticker)) {
      rows.set(ticker, {
        ticker, fills: [], withdrawn: null, view: null, debate: null, ruling: null, proposal: null, repeats: 0,
        check: null, risk: null, order: null,
        ...(checkin ? { checkin: true } : {}),          // a close meeting's rows are exactly what they always were
      });
    }
    return rows.get(ticker);
  };
  events.forEach((event, index) => {
    const said = event.payload || {};
    if (event.kind === "proposals" || event.kind === "checkin_decisions") {
      // The portfolio manager speaks once for all tickers: each proposal belongs to its own ticker's trail.
      for (const item of (event.kind === "proposals" ? said.proposals : said.decisions) || []) {
        const ticker = tickerOf(item.ticker);
        if (!ticker) continue;
        const row = of(ticker);
        if (row.proposal) row.repeats += 1;
        else row.proposal = { index, action: item.action, confidence: item.confidence };
      }
      return;
    }
    if (!event.ticker) return;
    const row = of(event.ticker);
    if (event.kind === "fill") {
      row.fills.push({ index, side: said.side, shares: said.shares, price: said.price, pnl_pct: said.pnl_pct ?? null });
    } else if (event.kind === "view") {
      row.view = { index, direction: said.direction, confidence: said.confidence, held: Boolean(said.held) };
    } else if (event.kind === "view_update") {
      row.view = { index, effect: said.effect };
    } else if (event.kind === "argument") {
      row.debate = row.debate || { index, lines: [] };
      row.debate.lines.push({ index, round: said.round || 1 });
    } else if (event.kind === "ruling") {
      row.ruling = { index, stance: said.stance, confidence: said.confidence };
    } else if (event.kind === "accepted" || event.kind === "rejected") {
      if (said.source === "default") {
        if (row.check) row.check.kept = index;
        else row.check = { index, status: "accepted", action: said.action, source: "default", kept: null };
      } else if (!row.check) {
        row.check = { index, status: event.kind, action: said.action, source: "portfolio_manager", kept: null };
      }                                                    // else: the check of a repeated proposal, which never counts
    } else if (event.kind === "risk_approved" || event.kind === "risk_reduced" || event.kind === "risk_rejected") {
      const decision = said.decision || {}, request = said.request || {};
      row.risk = {
        index, verdict: decision.verdict || event.kind.replace("risk_", ""), side: decision.side || request.side,
        requested: decision.requested_shares ?? request.shares ?? null, approved: decision.approved_shares ?? null,
      };
    } else if (event.kind === "order_placed") {
      row.order = { index, side: said.side, shares: said.shares };
    } else if (event.kind === "order_withdrawn") {
      row.withdrawn = { index, side: said.side, shares: said.shares };
      // At a check-in, taking a waiting buy back is the decision's last step, where an order would be.
      if (checkin) row.order = { index, side: said.side, shares: said.shares, withdrawn: true };
    } else if (event.kind === "noted") {
      row.order = { index, noted: said.action };
    }
  });
  return [...rows.values()];
}

/** Whether a row's last step is an order that was really placed: not one taken back, nor a decision only noted. */
const placed = (order) => Boolean(order) && !order.withdrawn && !order.noted;

/** Whether a step has happened by line `index`. A debate has, once its first argument is made. */
export function reached(step, index = Infinity) {
  return Boolean(step) && step.index <= index;
}

/**
 * The latest line of a step that has been said by line `upTo`: where clicking
 * the step leads, and whose words its tooltip shows. A debate has several
 * lines; a check has two when a turned-back proposal is followed by the
 * default that keeps the holding. Ask only about a step that has been reached.
 */
export function lineAt(name, step, upTo = Infinity) {
  if (name === "debate") return Math.max(...step.lines.filter((line) => line.index <= upTo).map((line) => line.index));
  if (name === "check" && step.kept !== null && step.kept <= upTo) return step.kept;
  return step.index;
}

/** The last line that belongs to a step. */
export const lineOf = (name, step) => lineAt(name, step, Infinity);

/** Whether line `index` is one of the step's own lines. */
export function isLine(name, step, index) {
  if (name === "debate") return step.lines.some((line) => line.index === index);
  return step.index === index || (name === "check" && step.kept === index);
}

/** The first line of a ticker's decision today, or null when its row holds none (only old orders filling). */
export function firstLine(row) {
  const lines = STEPS.filter((name) => row[name]).map((name) => row[name].index);
  return lines.length ? Math.min(...lines) : null;
}

/** Whether the holding was kept by the checks' default by line `index`. */
const keptBy = (check, index) => check.source === "default" || (check.kept !== null && check.kept <= index);

/**
 * Where a ticker stands by line `index`: the furthest step it has reached and
 * what that step said. `Infinity` asks for where it ended.
 */
export function outcome(row, index = Infinity) {
  const seen = (step) => reached(step, index);
  // Said only of a check-in's rows, so a close meeting's outcomes are exactly what they always were.
  const checkin = row.checkin ? { checkin: true } : {};
  if (seen(row.order)) {
    if (row.order.noted) return { kind: "noted", action: row.order.noted };
    if (row.order.withdrawn) return { kind: "withdrawn", shares: row.order.shares };
    return { kind: "ordered", side: row.order.side, shares: row.order.shares };
  }
  if (seen(row.risk)) {
    if (row.risk.verdict === "rejected") return { kind: "risk_rejected", side: row.risk.side };
    return { kind: "sized", verdict: row.risk.verdict, side: row.risk.side, shares: row.risk.approved };
  }
  if (seen(row.check)) {
    if (row.check.source === "default") return { kind: "kept", source: "default", ...checkin };
    if (row.check.status === "rejected") {
      return { kind: "check_rejected", action: row.check.action, kept: keptBy(row.check, index), ...checkin };
    }
    if (!MOVES.has(row.check.action)) return { kind: "kept", source: "portfolio_manager", ...checkin };
    return { kind: "accepted", action: row.check.action };
  }
  if (seen(row.proposal)) return { kind: "proposed", action: row.proposal.action };
  if (seen(row.ruling)) return { kind: "ruled", stance: row.ruling.stance };
  if (seen(row.debate)) return { kind: "debating" };
  if (seen(row.view)) {
    return row.view.effect ? { kind: "updated", effect: row.view.effect } : { kind: "viewed", direction: row.view.direction };
  }
  return { kind: "waiting" };
}

/**
 * The outcome in words. Only fixed vocabulary and recorded numbers: no
 * sentence of anyone's is rewritten here. `ended` says the record is over at
 * this point (the playback is at the last line of a run that has finished):
 * only then is a ticker said to have got no further.
 */
export function outcomeText(result, ended = false) {
  const more = ended ? "，之后没有进一步动作" : "";
  switch (result.kind) {
    case "ordered": return `已下单：${word("side", result.side)} ${result.shares} 股`;
    case "noted": return `记下，留给收盘会决定：${word("action", result.action)}`;
    case "withdrawn": return `已撤回等待成交的买单：${result.shares} 股`;
    case "risk_rejected": return "风控拒绝，没有下单";
    case "sized":
      return `风控${word("verdict", result.verdict)}：${word("side", result.side)} ${result.shares} 股${ended ? "，这一次没有新的下单记录" : ""}`;
    case "check_rejected":
      if (result.checkin) return `${word("action", result.action)}的决定被规则退回${result.kept ? "，保持原样" : ""}`;
      return `${word("action", result.action)}的提案被规则退回${result.kept ? "，按规则继续持有" : ""}`;
    case "kept":
      if (result.checkin) return result.source === "default" ? "没有决定，按规则保持原样" : "保持原样，不动";
      return result.source === "default" ? "没有提案，按规则继续持有" : "继续持有，不动";
    case "accepted": return `${word("action", result.action)}的提案通过检查${more}`;
    case "proposed": return `组合经理提出${word("action", result.action)}${more}`;
    case "ruled": return `裁判裁定${word("ownership", result.stance)}${more}`;
    case "debating": return ended ? "辩论过，没有裁决" : "正在辩论";
    case "viewed": return `分析师${word("direction", result.direction)}${more}`;
    case "updated": return `分析师认为原来的观点${word("effect", result.effect)}${more}`;
    default: return "还没轮到";
  }
}

/**
 * How many tickers have got how far by line `index`: views written, debates
 * ruled on, proposals that would change the book, those the checks let
 * through, those the risk gate sized, and orders placed. `kept` counts the
 * holdings the checks left as they are, `turned_back` the proposals and
 * orders that were refused. A repeated proposal is never counted.
 */
export function funnel(trail, index = Infinity) {
  const count = (test) => trail.filter(test).length;
  const seen = (step) => reached(step, index);
  const accepted = (row) => seen(row.check) && row.check.status === "accepted";
  return {
    views: count((row) => seen(row.view)),
    ruled: count((row) => seen(row.ruling)),
    proposed: count((row) => seen(row.proposal) && MOVES.has(row.proposal.action)),
    accepted: count((row) => accepted(row) && MOVES.has(row.check.action)),
    sized: count((row) => seen(row.risk) && row.risk.verdict !== "rejected"),
    ordered: count((row) => seen(row.order) && placed(row.order)),
    kept: count((row) => seen(row.check) && (keptBy(row.check, index) || (accepted(row) && !MOVES.has(row.check.action)))),
    turned_back: count((row) => (seen(row.check) && row.check.status === "rejected")
      || (seen(row.risk) && row.risk.verdict === "rejected")),
  };
}

/**
 * What a step says, in a few characters, as far as the record has got by line
 * `upTo`. `brief` is for the trail's narrow cells; the full wording is for
 * its tooltips.
 */
export function stepText(name, step, { brief = false, upTo = Infinity } = {}) {
  switch (name) {
    case "view":
      if (step.effect) return word("effect", step.effect);
      return `${word("direction", step.direction)} ${two(step.confidence)}`.trim();
    case "debate": {
      const said = step.lines.filter((line) => line.index <= upTo);
      const rounds = Math.max(0, ...said.map((line) => line.round));
      return brief ? `${rounds} 轮` : `${rounds} 轮 · ${said.length} 次发言`;
    }
    case "ruling": return `${word("ownership", step.stance)} ${two(step.confidence)}`.trim();
    case "proposal": return `${word("action", step.action)} ${two(step.confidence)}`.trim();
    case "check":
      if (step.source === "default") return `默认${word("action", step.action)}`;
      if (step.status === "rejected" && keptBy(step, upTo)) return brief ? "拒绝·默认持有" : "拒绝，按规则继续持有";
      return word("check", step.status);
    case "risk":
      if (step.verdict === "rejected") return "拒绝";
      if (step.verdict === "reduced") return brief ? `削减到 ${step.approved} 股` : `削减 ${step.requested}→${step.approved} 股`;
      return `批准 ${step.approved} 股`;
    case "order":
      if (step.noted) return brief ? `记下·${word("action", step.noted)}` : `记下，留给收盘会：${word("action", step.noted)}`;
      if (step.withdrawn) return brief ? `撤回 ${step.shares} 股` : `撤回等待成交的买单 ${step.shares} 股`;
      return `${word("side", step.side)} ${step.shares} 股`;
    default: return "";
  }
}

/** Which way a step leans, for an arrow beside its words: up, down or neither. */
export function lean(name, step) {
  if (name === "view") {
    if (step.effect) return { strengthened: "up", weakened: "down", broken: "down" }[step.effect] || "flat";
    return { bullish: "up", bearish: "down" }[step.direction] || "flat";
  }
  if (name === "ruling") return step.stance === "own" ? "up" : "down";
  if (name === "order" && step.withdrawn) return "stop";                  // a buy that will not happen
  if (name === "order" && step.noted) return MOVES.has(step.noted) ? "down" : "flat";
  if (name === "proposal" || name === "order") {
    const move = name === "order" ? step.side : step.action;
    return move === "buy" ? "up" : MOVES.has(move) ? "down" : "flat";
  }
  if (name === "check") return step.status === "rejected" ? "stop" : "flat";
  if (name === "risk") return step.verdict === "rejected" ? "stop" : "flat";
  return "flat";
}

/**
 * What each figure has concluded by line `index`, in a few characters, for the
 * sign above its head: the latest thing it said, or a count of what it did.
 * `board` is the macro view the pipeline computed, once it has.
 */
export function standings(events, index) {
  const signs = {};
  let board = null;
  const checks = { accepted: 0, rejected: 0 }, gate = { approved: 0, reduced: 0, rejected: 0 };
  const desk = { fills: 0, orders: 0, noted: 0, withdrawn: 0 };
  const checkin = events.some((event) => event.kind === "briefing");
  events.slice(0, index + 1).forEach((event) => {
    const said = event.payload || {};
    if (event.kind === "assessment" && MACRO.has(event.speaker)) {
      signs[event.speaker] = {
        text: `${word("stance", said.stance)} ${two(said.confidence)}`.trim(),
        lean: { supportive: "up", restrictive: "down" }[said.stance] || "flat",
      };
    } else if (event.kind === "macro_view" || event.kind === "macro_view_reused") {
      board = {
        text: `${word("appetite", said.risk_appetite)} ${two(said.confidence)}`.trim(),
        lean: { risk_on: "up", risk_off: "down" }[said.risk_appetite] || "flat",
        reused: event.kind === "macro_view_reused" ? said.session_date || null : null,
      };
    } else if (event.kind === "view") {
      signs.ticker_analyst = {
        text: `${event.ticker} ${word("direction", said.direction)} ${two(said.confidence)}`.trim(),
        lean: { bullish: "up", bearish: "down" }[said.direction] || "flat",
      };
    } else if (event.kind === "argument") {
      signs[event.speaker] = { text: `${event.ticker} 第 ${said.round || 1} 轮`, lean: "flat" };
    } else if (event.kind === "ruling") {
      signs.judge = {
        text: `${event.ticker} ${word("ownership", said.stance)} ${two(said.confidence)}`.trim(),
        lean: said.stance === "own" ? "up" : "down",
      };
    } else if (event.kind === "proposals") {
      // A count of the manager's own list as it was written, repeats included: this is what it said.
      const tally = {};
      for (const item of said.proposals || []) tally[item.action] = (tally[item.action] || 0) + 1;
      const parts = ["buy", "sell", "trim", "hold"].filter((action) => tally[action])
        .map((action) => `${word("action", action)} ${tally[action]}`);
      signs.portfolio_manager = { text: parts.length ? parts.join(" · ") : "没有提案", lean: "flat" };
    } else if (event.kind === "accepted" || event.kind === "rejected") {
      checks[event.kind] += 1;
      signs.risk_officer = { text: `通过 ${checks.accepted} · 退回 ${checks.rejected}`, lean: "flat" };
    } else if (event.kind.startsWith("risk_") && event.kind.slice(5) in gate) {
      gate[event.kind.slice(5)] += 1;
      signs.risk_officer = { text: `批准 ${gate.approved} · 削减 ${gate.reduced} · 拒绝 ${gate.rejected}`, lean: "flat" };
    } else if (checkin && (event.kind === "noted" || event.kind === "order_withdrawn" || event.kind === "order_placed")) {
      desk[{ noted: "noted", order_withdrawn: "withdrawn", order_placed: "orders" }[event.kind]] += 1;
      signs.trader = { text: `留给收盘会 ${desk.noted} · 撤回 ${desk.withdrawn} · 下单 ${desk.orders}`, lean: "flat" };
    } else if (event.kind === "fill" || event.kind === "order_placed") {
      desk[event.kind === "fill" ? "fills" : "orders"] += 1;
      signs.trader = { text: `成交 ${desk.fills} · 下单 ${desk.orders}`, lean: "flat" };
    } else if (event.kind === "view_update") {
      signs.ticker_analyst = {
        text: `${event.ticker} ${word("effect", said.effect)}`,
        lean: { strengthened: "up", weakened: "down", broken: "down" }[said.effect] || "flat",
      };
    } else if (event.kind === "checkin_decisions") {
      // A count of the manager's own list as it was written, repeats included: this is what it said.
      const tally = {};
      for (const item of said.decisions || []) tally[item.action] = (tally[item.action] || 0) + 1;
      const parts = ["keep", "trim", "sell", "withdraw"].filter((action) => tally[action])
        .map((action) => `${word("action", action)} ${tally[action]}`);
      signs.portfolio_manager = { text: parts.length ? parts.join(" · ") : "没有决定", lean: "flat" };
    }
  });
  return { signs, board };
}

/** The last thing a figure has said by line `index`, or null when it has not spoken yet. */
export function lastSaid(events, index, speaker) {
  for (let at = Math.min(index, events.length - 1); at >= 0; at -= 1) {
    if (events[at].speaker === speaker) return events[at];
  }
  return null;
}

/**
 * What became of a ticker on one day, for one cell of the grid that sets the
 * sessions side by side. `cell` is that day's entry from the signal table.
 * "风控放行" is an order that passed the risk gate, as the table lists it.
 */
export function fateText(cell) {
  if (cell.ordered) return `${word("side", cell.ordered.side)} ${cell.ordered.shares} 股，风控放行`;
  if (!cell.action) return "";
  if (cell.verdict === "rejected") return `${word("action", cell.action)}，风控拒绝`;
  if (cell.check === "rejected") return `${word("action", cell.action)}被规则退回${cell.kept_by_default ? "，默认持有" : ""}`;
  if (cell.action === "hold") return cell.source === "default" ? "没有提案，默认持有" : "持有";
  return `${word("action", cell.action)}，没有下单`;
}

/**
 * The record cut into stretches for the timeline: one per run of lines of the
 * same stage and, inside a debate, of the same ticker.
 */
export function stretches(events) {
  const found = [];
  events.forEach((event, index) => {
    const ticker = event.stage === "debate" ? event.ticker || null : null;
    const last = found[found.length - 1];
    if (last && last.stage === event.stage && last.ticker === ticker) {
      last.to = index;
    } else {
      found.push({ stage: event.stage, title: event.stage_title || event.stage, ticker, from: index, to: index });
    }
  });
  return found;
}
