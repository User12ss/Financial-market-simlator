// An answer of /api/plans, shaped as the server sends it: UPA on its second version, with a condition that was
// dropped after it had trimmed, one found by the watch and waiting for the close, and one a headline that was
// heard; and a plan for UPZ that is over. The recorded sentences are as the desk writes them.

const condition = (kind, fields = {}) => ({ kind, level: null, average: null, on: null, ending: null, keywords: [], ...fields });
const row = (key, cond, action, fields = {}) => ({
  key, condition: cond, action, new_stop: null, reason: `Reason for ${key}.`, source: "manager", automatic: true,
  in_version: 2, dropped: false, state: "waiting", trigger: null, ...fields,
});
const trigger = (fields) => ({
  version: 1, fired_at: "2026-11-17T15:00:00Z", session_date: "2026-11-17", found_by: "watch", evidence: "It traded at 402.74.",
  result: "", handled_by: "", handled_on: null, ...fields,
});
const version = (number, fields = {}) => ({
  version: number, session_date: "2026-11-13", created_at: "2026-11-13T23:30:00Z", source: "manager", held: false,
  thesis: "The trend carries it.", direction: "bullish", horizon: 10, ends_on: "2026-11-30", close: 406.2905, stop: 400.83,
  target: null, chances: {}, changes: [], basis: "There is no plan for it yet.", refused: [], data_warnings: [], ...fields,
});

export const CONDITIONS = [
  row("time", condition("date", { on: "2026-11-30" }), "exit", { source: "rule", reason: "The time the trade was given has run out without it reaching its stop." }),
  row("wrong", condition("price_below", { level: 404.65 }), "exit", { state: "fired", trigger: trigger({}) }),
  row("2", condition("price_above", { level: 412.4 }), "tighten_stop", { new_stop: 403.56 }),
  row("3", condition("close_below_average", { average: 50 }), "trim"),
  row("4", condition("chance_above", { ending: "bad", level: 0.6 }), "call_meeting", { automatic: false }),
  row("5", condition("headline", { keywords: ["subpoena", "recall"] }), "call_meeting", {
    automatic: false, state: "heard",
    trigger: trigger({ fired_at: "2026-11-16T15:00:00Z", session_date: "2026-11-16", evidence: "A headline has 'subpoena': UPA Corp receives a subpoena over its accounts",
      result: "Heard at the called check-in of 2026-11-16 10:05 New York time.", handled_by: "m-1", handled_on: "2026-11-16" }),
  }),
  row("6", condition("chance_below", { ending: "good", level: 0.2 }), "withdraw", { state: "not_done",
    trigger: trigger({ found_by: "close", fired_at: "2026-11-17T23:30:00Z", evidence: "The chance of ending at the target is 15%.",
      result: "The buy had filled.", handled_by: "run-17", handled_on: "2026-11-17" }) }),
  row("1", condition("date", { on: "2026-11-17" }), "trim", { in_version: 1, dropped: true, state: "done",
    reason: "Half off after two sessions.",
    trigger: trigger({ found_by: "close", fired_at: "2026-11-17T23:30:00Z", evidence: "The session of 2026-11-17 has closed.",
      result: "A sale of 12 share(s) is placed for the next open.", handled_by: "run-17", handled_on: "2026-11-17" }) }),
];

export const PLANS = {
  arm: "desk/fake/a", has_plans: true,
  standing: [{
    ticker: "UPA", opened: "2026-11-13", held_now: true,
    versions: [
      version(1),
      version(2, {
        session_date: "2026-11-17", held: true, close: 408.73, target: 431.5, chances: { good: 0.34, bad: 0.21, middle: 0.45 },
        changes: ["Dropped: [1] On the close of 2026-11-17: sell half, by itself. Half off after two sessions."],
        basis: "Its condition [1] held on 2026-11-17: The session of 2026-11-17 has closed. The manager: The trim is done.",
        refused: ["Condition 2 (chance_below, exit): one of the simulation's chances may call a meeting and nothing else."],
        data_warnings: ["UPA: the 50-day average is missing."],
      }),
    ],
    conditions: CONDITIONS,
  }, {
    ticker: "UPB", opened: "2026-11-17", held_now: false,
    versions: [version(1, { session_date: "2026-11-17", source: "default", thesis: "The trend is intact and nothing argues against it.",
      refused: ["The plan is refused whole: it does not say what would show that the plan is wrong."] })],
    conditions: [CONDITIONS[0]],
  }],
  ended: [{
    ticker: "UPZ", opened: "2026-11-13", closed_on: "2026-11-18", closed_why: "The position was sold at the open on 2026-11-18, at 402.54.",
    versions: 1, last_version: version(1),
    conditions: [CONDITIONS[0], { ...CONDITIONS[1], state: "done" }],
  }],
};
