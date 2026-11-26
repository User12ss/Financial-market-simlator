// What the record says about the meeting as a whole, worked out without a
// page: who has spoken to whom, which stages it went through, and where each
// ticker ended. Nothing here is anyone's sentence: counts, the pipeline's own
// vocabulary, and the places of lines in the record.

import { STEPS, buildTrail, funnel, lineOf, outcome, outcomeText, reached, standings } from "./decisions.js";

export const EVERYONE = "all";

/**
 * The lines up to `index`, counted by who spoke to whom.
 *  - `pairs`: one entry per speaker and listener, most lines first; `last` is the place of the latest one.
 *  - `calls`: how many lines each speaker addressed to everyone.
 *  - `said`: how many lines each speaker has said in all.
 * A line with no speaker, or one addressed to its own speaker, joins nobody.
 */
export function exchanges(events, index = Infinity) {
  const pairs = new Map(), calls = new Map(), said = new Map();
  const upTo = Math.min(events.length - 1, index);
  for (let at = 0; at <= upTo; at += 1) {
    const { speaker: from, to } = events[at];
    if (!from) continue;
    said.set(from, (said.get(from) || 0) + 1);
    if (!to || to === from) continue;
    if (to === EVERYONE) {
      calls.set(from, (calls.get(from) || 0) + 1);
      continue;
    }
    const key = `${from}>${to}`;
    const pair = pairs.get(key) || { from, to, count: 0, last: at };
    pair.count += 1;
    pair.last = at;
    pairs.set(key, pair);
  }
  return { pairs: [...pairs.values()].sort((a, b) => b.count - a.count || b.last - a.last), calls, said };
}

/**
 * The stages of the record in the order each first appears, with where its
 * lines begin and end. `done` counts its lines up to `index`; `state` says
 * whether the line being played is in it, behind it or still ahead.
 */
export function stagesOf(events, index = -1) {
  const found = new Map();
  events.forEach((event, at) => {
    const stage = found.get(event.stage) || { stage: event.stage, title: event.stage_title || event.stage, from: at, to: at, count: 0, done: 0 };
    stage.to = at;
    stage.count += 1;
    if (at <= index) stage.done += 1;
    found.set(event.stage, stage);
  });
  const now = index >= 0 && index < events.length ? events[index].stage : null;
  return [...found.values()].map((stage) => ({
    ...stage, state: stage.stage === now ? "now" : stage.done === stage.count ? "done" : stage.done > 0 ? "part" : "ahead",
  }));
}

const ACTED = new Set(["ordered", "withdrawn", "noted", "sized"]);
const STOPPED = new Set(["risk_rejected", "check_rejected"]);
const KEPT = new Set(["kept"]);
const SELLS = new Set(["sell", "trim", "withdraw"]);

/** Which way an outcome leans, for its mark: a buy up, a sale down, a refusal stopped, the rest neither. */
function leanOf(result) {
  if (STOPPED.has(result.kind)) return "stop";
  const moved = result.side || result.action;
  if (result.kind === "withdrawn") return "down";
  if (ACTED.has(result.kind) && moved) return SELLS.has(moved) ? "down" : moved === "buy" ? "up" : "flat";
  return "flat";
}

/** The last line of the furthest step a ticker reached: where its outcome was said. Null when it has no step. */
export function decisiveLine(row) {
  const name = [...STEPS].reverse().find((step) => reached(row[step]));
  return name ? lineOf(name, row[name]) : null;
}

/**
 * Where the meeting ended, for reading at a glance: the macro view, how many
 * tickers got how far, and each ticker's outcome in the trail's own words,
 * sorted into what was acted on, what was turned back, what was kept as it
 * is, and what got no further than talk. `ended` says the record is over.
 */
export function summaryOf(events, ended = true) {
  const trail = buildTrail(events);
  const groups = { acted: [], stopped: [], kept: [], open: [] };
  for (const row of trail) {
    const result = outcome(row);
    if (result.kind === "waiting") continue;                 // only old orders filling: no decision of this meeting
    const item = { ticker: row.ticker, kind: result.kind, text: outcomeText(result, ended), lean: leanOf(result), line: decisiveLine(row) };
    (ACTED.has(result.kind) ? groups.acted : STOPPED.has(result.kind) ? groups.stopped : KEPT.has(result.kind) ? groups.kept : groups.open).push(item);
  }
  return { board: standings(events, Infinity).board, funnel: funnel(trail), groups, tickers: trail.length };
}
