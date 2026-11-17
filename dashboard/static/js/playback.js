// What is on show in the meeting room and how it moves on: which line, whether
// it is playing, whether it follows a run that is still going. Kept apart from
// the drawing, with no page in sight, so it can be tested on its own.

export function newPlayback() {
  return { meeting: null, events: [], index: -1, playing: false, speed: 1, follow: false };
}

const runOf = (state) => (state.meeting && state.meeting.run) || null;

/** Whether the run on show is still going. */
export const isLive = (state) => Boolean(runOf(state) && runOf(state).live);

export const lastIndex = (state) => state.events.length - 1;

export const current = (state) => (state.index >= 0 ? state.events[state.index] : null);

/**
 * Put a session's record on show. A different run starts at its first line,
 * or at its newest when the run is still going. The same run again keeps the
 * reader's place. Returns whether it was the same run.
 */
export function load(state, meeting) {
  const before = runOf(state);
  const same = Boolean(before && meeting.run && before.run_id === meeting.run.run_id);
  state.meeting = meeting;
  state.events = meeting.events.slice();
  if (!same) {
    state.playing = false;
    state.follow = isLive(state);
    state.index = state.follow ? lastIndex(state) : Math.min(0, lastIndex(state));
  } else if (state.follow) {
    state.index = lastIndex(state);
  } else {
    state.index = Math.min(state.index, lastIndex(state));
  }
  return same;
}

/**
 * Add lines recorded since the last look. Only lines of the run on show are
 * taken, each once and in order, whatever is handed in. Returns how many.
 */
export function extend(state, runId, events) {
  const run = runOf(state);
  if (!run || run.run_id !== runId) return 0;
  let seen = state.events.length ? state.events[state.events.length - 1].seq : 0;
  let added = 0;
  for (const event of events) {
    if (event.seq <= seen) continue;
    state.events.push(event);
    seen = event.seq;
    added += 1;
  }
  if (added && state.follow) state.index = lastIndex(state);
  return added;
}

/** Go to a line. Doing so by hand stops the playing, and stops the following unless it is the newest line. */
export function show(state, index, byHand = false) {
  state.index = Math.max(-1, Math.min(lastIndex(state), index));
  if (byHand) {
    state.playing = false;
    state.follow = state.follow && state.index === lastIndex(state);
  }
}

/** Start playing. From the top when the last line is already on show. Playing and following exclude each other. */
export function start(state) {
  if (state.events.length === 0) return false;
  if (state.index >= lastIndex(state)) state.index = -1;
  state.playing = true;
  state.follow = false;
  return true;
}

export function pause(state) {
  state.playing = false;
}

export function setFollow(state, on) {
  state.follow = on;
  if (on) {
    state.playing = false;
    state.index = lastIndex(state);
  }
}

/**
 * One step of playing. "shown": the next line is now on show. "waiting": at
 * the end of a run still going, so the next line has not been said yet.
 * "ended": nothing more to play.
 */
export function advance(state) {
  if (!state.playing) return "ended";
  if (state.index >= lastIndex(state)) {
    if (isLive(state)) return "waiting";
    state.playing = false;
    return "ended";
  }
  state.index += 1;
  return "shown";
}

/** How long a line stays up while playing: longer for a longer line, within bounds, shortened by the speed. */
export function wait(event, speed) {
  const reading = 1400 + 38 * (event ? event.remark.length : 0);
  return Math.min(8000, Math.max(1600, reading)) / speed;
}
