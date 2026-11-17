// Keeping the page in step with the server while a run is going: what the one
// live connection has heard, and how a meeting room is fed from it. No page in
// sight here, so these rules can be tested on their own.

/** What the live connection has heard so far: the arm's newest run and everything said in it. */
export function newLive() {
  return { run: null, session: null, events: [], heard: false };
}

/**
 * Take a message about a run. Lines heard so far belong to the run before, so
 * a new run starts with none. Returns whether pages that show numbers should
 * read them again: yes for every such message but the first one after
 * connecting, which only says where things stand.
 */
export function hearRun(live, told) {
  if (told.new) live.events = [];
  live.run = told.run;
  live.session = told.session;
  const changed = live.heard;
  live.heard = true;
  return changed;
}

export function hearEvent(live, told) {
  live.events.push(told.event);
}

/** After the connection was lost and found again, the server tells everything from the start. */
export function reconnected(live) {
  live.heard = false;
}

/**
 * Keeps one meeting room in step with the server.
 *
 * `open(target)` says what the room is for: {} is the latest session,
 * { session } the run shown for that day, { session, run_id } one particular
 * run. `hear(message)` takes what the live connection says. The room itself
 * accepts only lines of the run it is showing, each once (see playback.js), so
 * everything heard can be offered to it.
 */
export function roomFeed({
  room, fetchMeeting, live, onProblem = () => {}, delay = 1500,
  timers = { set: (work, ms) => setTimeout(work, ms), clear: (id) => clearTimeout(id) },
}) {
  let target = {};
  let asked = 0;                // how often the record has been asked for: only the last answer counts
  let shown = null;             // the answer now in the room, so the room is left alone when nothing has changed
  let later = null;

  function merge() {
    const now = live();
    return now.run ? room.extend(now.run.run_id, now.events) : false;
  }

  async function reload() {
    const mine = ++asked;
    const answer = await fetchMeeting(target);
    if (mine !== asked) return false;                 // asked again since: this answer is already out of date
    onProblem(answer.ok ? null : (answer.data && answer.data.error) || "读不到这一场的记录。");
    if (!answer.ok) return false;
    const text = JSON.stringify(answer.data);
    if (text !== shown) {
      shown = text;
      room.load(answer.data);
    }
    merge();
    return true;
  }

  function open(next) {
    target = next || {};
    shown = null;
    return reload();
  }

  /** Whether a message about a run says something this room does not show yet. */
  function concerns(message) {
    const differs = message.run.run_id !== room.runId() || message.run.status !== room.status();
    if (target.run_id) return message.run.run_id === target.run_id && differs;
    if (target.session) return message.session === target.session && differs;
    return differs;
  }

  function hear(message) {
    if (message.type === "run") {
      if (concerns(message)) reload();
      return;
    }
    if (!merge()) return;
    timers.clear(later);        // the per-person lines are counted by the server: fetch them once things settle
    later = timers.set(reload, delay);
  }

  function stop() {
    timers.clear(later);
    asked += 1;                 // an answer still on its way is dropped
  }

  return { open, reload, hear, stop, target: () => target };
}
