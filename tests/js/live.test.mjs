// Keeping a meeting room in step with the server while a run is going: run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { hearEvent, hearRun, newLive, reconnected, roomFeed } from "../../dashboard/static/js/live.js";
import { extend, load, newPlayback } from "../../dashboard/static/js/playback.js";

const line = (seq) => ({ seq, remark: `Line ${seq}.` });
const lines = (...seqs) => seqs.map(line);
const run = (id, status = "running") => ({ run_id: id, status, live: status === "running" });
const about = (id, status, session = "2026-11-16", fresh = false) =>
  ({ type: "run", run: run(id, status), session, new: fresh });
const said = (seq) => ({ type: "event", event: line(seq) });
const meeting = (id, status, events, session = "2026-11-16") =>
  ({ ok: true, data: { session, run: run(id, status), events } });

/** A room without a page: the real playback rules, and a count of how often it was loaded. */
function room() {
  const state = newPlayback();
  const self = {
    state, loads: 0,
    load: (record) => { self.loads += 1; load(state, record); },
    extend: (runId, events) => extend(state, runId, events) > 0,
    runId: () => (state.meeting && state.meeting.run ? state.meeting.run.run_id : null),
    status: () => (state.meeting && state.meeting.run ? state.meeting.run.status : null),
    seqs: () => state.events.map((event) => event.seq),
  };
  return self;
}

/** A server whose answers are handed out by the test, in whatever order the test likes. */
function server() {
  const waiting = [];
  return {
    asked: [],
    fetchMeeting(target) {
      this.asked.push(target);
      return new Promise((resolve) => waiting.push(resolve));
    },
    answer(index, value) { waiting[index](value); },
    pending: () => waiting.length,
  };
}

/** Timers that go off only when the test says so. */
function clock() {
  const set = [];
  return {
    timers: { set: (work) => set.push(work) - 1, clear: (id) => { if (id !== null && id !== undefined) set[id] = null; } },
    due: () => set.filter(Boolean).length,
    fire: () => { const work = set.filter(Boolean); set.length = 0; return Promise.all(work.map((item) => item())); },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

function fed({ target = {} } = {}) {
  const live = newLive();
  const parts = { live, room: room(), server: server(), clock: clock(), problems: [] };
  parts.feed = roomFeed({
    room: parts.room, live: () => live, fetchMeeting: (wanted) => parts.server.fetchMeeting(wanted),
    onProblem: (text) => parts.problems.push(text), timers: parts.clock.timers,
  });
  parts.opened = parts.feed.open(target);
  return parts;
}

test("what the connection hears: a new run starts with no lines, and only later messages mean a change", () => {
  const live = newLive();
  assert.equal(hearRun(live, about("r1", "running", "2026-11-13", true)), false);      // where things stand
  hearEvent(live, said(1));
  hearEvent(live, said(2));
  assert.deepEqual([live.run.run_id, live.session, live.events.length], ["r1", "2026-11-13", 2]);

  assert.equal(hearRun(live, about("r1", "ok", "2026-11-13")), true);                  // it ended: numbers changed
  assert.equal(live.events.length, 2);                                                 // the same run keeps its lines
  assert.equal(hearRun(live, about("r2", "running", "2026-11-16", true)), true);
  assert.deepEqual([live.run.run_id, live.session, live.events], ["r2", "2026-11-16", []]);

  // The connection dropped and came back: the server tells everything again, and that is not a change.
  reconnected(live);
  assert.equal(hearRun(live, about("r2", "running", "2026-11-16", true)), false);
  assert.equal(hearRun(live, about("r2", "ok", "2026-11-16")), true);
});

test("lines heard before the record arrived are in the room once it does, each once", async () => {
  const { live, room: shown, server: answers, feed, opened } = fed();
  hearRun(live, about("r1", "running", "2026-11-16", true));
  for (const seq of [1, 2, 3]) {
    hearEvent(live, said(seq));
    feed.hear(said(seq));                             // nothing to add them to yet
  }
  assert.deepEqual(shown.seqs(), []);
  answers.answer(0, meeting("r1", "running", lines(1, 2)));          // the server's answer is one line behind
  assert.equal(await opened, true);
  assert.deepEqual(shown.seqs(), [1, 2, 3]);
  hearEvent(live, said(4));
  feed.hear(said(4));
  assert.deepEqual(shown.seqs(), [1, 2, 3, 4]);
});

test("an answer that arrives after a newer one was asked for is dropped", async () => {
  const { live, room: shown, server: answers, feed, opened } = fed();
  hearRun(live, about("r2", "running", "2026-11-16", true));
  feed.hear(about("r2", "running", "2026-11-16", true));             // the room shows nothing yet: ask again
  assert.equal(answers.pending(), 2);

  answers.answer(1, meeting("r2", "running", lines(1)));             // the newer question is answered first
  await settle();
  assert.equal(shown.runId(), "r2");
  answers.answer(0, meeting("r1", "ok", lines(1, 2, 3)));            // then the older one, with the run before
  assert.equal(await opened, false);
  assert.deepEqual([shown.runId(), shown.seqs(), shown.loads], ["r2", [1], 1]);

  // So the room still takes the new run's lines.
  hearEvent(live, said(1));
  hearEvent(live, said(2));
  feed.hear(said(2));
  assert.deepEqual(shown.seqs(), [1, 2]);
});

test("lines of a run the room is not showing are never put into it", async () => {
  const { live, room: shown, server: answers, feed, opened, clock: timer } = fed();
  answers.answer(0, meeting("standing", "ok", lines(1, 2)));
  await opened;
  hearRun(live, about("failed-later", "failed", "2026-11-16", true));
  hearEvent(live, said(3));
  feed.hear(said(3));
  assert.deepEqual([shown.seqs(), timer.due()], [[1, 2], 0]);
});

test("the latest session's room asks again when the run changes or ends, and not otherwise", async () => {
  const { live, room: shown, server: answers, feed, opened } = fed();
  answers.answer(0, meeting("r1", "running", lines(1)));
  await opened;

  hearRun(live, about("r1", "running"));
  feed.hear(about("r1", "running"));                  // nothing the room does not show
  assert.equal(answers.pending(), 1);

  feed.hear(about("r1", "ok"));                       // it ended
  assert.equal(answers.pending(), 2);
  answers.answer(1, meeting("r1", "ok", lines(1, 2)));
  await settle();
  assert.deepEqual([shown.status(), shown.seqs()], ["ok", [1, 2]]);

  feed.hear(about("r2", "running", "2026-11-17", true));              // another day's run has started
  assert.equal(answers.pending(), 3);
  assert.deepEqual(answers.asked, [{}, {}, {}]);
});

test("a day being replayed is asked for again only for news of that day", async () => {
  const { room: shown, server: answers, feed, opened } = fed({ target: { session: "2026-11-13" } });
  answers.answer(0, meeting("r1", "ok", lines(1, 2), "2026-11-13"));
  await opened;

  feed.hear(about("r9", "running", "2026-11-16", true));              // today's run: not this room's business
  assert.equal(answers.pending(), 1);
  feed.hear(about("r1", "ok", "2026-11-13"));                         // the same run, as shown
  assert.equal(answers.pending(), 1);
  feed.hear(about("r5", "running", "2026-11-13", true));              // the day is being run again
  assert.equal(answers.pending(), 2);
  assert.deepEqual(answers.asked[1], { session: "2026-11-13" });
  assert.equal(shown.runId(), "r1");
});

test("one particular run being replayed is asked for again only when that run changes", async () => {
  const target = { session: "2026-11-13", run_id: "r1" };
  const { server: answers, feed, opened } = fed({ target });
  answers.answer(0, meeting("r1", "running", lines(1), "2026-11-13"));
  await opened;

  feed.hear(about("r5", "running", "2026-11-13", true));              // another attempt at the same day
  assert.equal(answers.pending(), 1);
  feed.hear(about("r1", "interrupted", "2026-11-13"));
  assert.equal(answers.pending(), 2);
  assert.deepEqual(answers.asked[1], target);
  assert.deepEqual(feed.target(), target);
});

test("the room is loaded again only when the answer differs, so a reader is not disturbed for nothing", async () => {
  const { room: shown, server: answers, feed, opened } = fed();
  answers.answer(0, meeting("r1", "ok", lines(1, 2)));
  await opened;
  const again = feed.reload();
  answers.answer(1, meeting("r1", "ok", lines(1, 2)));
  assert.equal(await again, true);
  assert.equal(shown.loads, 1);
  const more = feed.reload();
  answers.answer(2, meeting("r1", "ok", lines(1, 2, 3)));
  await more;
  assert.deepEqual([shown.loads, shown.seqs()], [2, [1, 2, 3]]);
  // Opening something else always loads, even when the answer happens to read the same.
  const other = feed.open({ session: "2026-11-16" });
  answers.answer(3, meeting("r1", "ok", lines(1, 2, 3)));
  await other;
  assert.equal(shown.loads, 3);
});

test("after new lines the server is asked once more, once things have settled", async () => {
  const { live, room: shown, server: answers, feed, opened, clock: timer } = fed();
  answers.answer(0, meeting("r1", "running", lines(1)));
  await opened;
  hearRun(live, about("r1", "running", "2026-11-16", true));
  for (const seq of [1, 2, 3, 4]) {
    hearEvent(live, said(seq));
    feed.hear(said(seq));
  }
  assert.deepEqual([shown.seqs(), timer.due(), answers.pending()], [[1, 2, 3, 4], 1, 1]);     // one wait, not three
  timer.fire();
  assert.equal(answers.pending(), 2);
  // The server's answer is behind what the room already has: nothing is taken away.
  answers.answer(1, meeting("r1", "running", lines(1, 2)));
  await settle();
  assert.deepEqual(shown.seqs(), [1, 2, 3, 4]);
});

test("a record that cannot be read is said so, and the next good answer takes the notice away", async () => {
  const { room: shown, server: answers, feed, opened, problems } = fed();
  answers.answer(0, { ok: false, status: 500, data: { error: "账本读不了：locked" } });
  assert.equal(await opened, false);
  assert.deepEqual([problems, shown.loads], [["账本读不了：locked"], 0]);
  const again = feed.reload();
  answers.answer(1, { ok: false, status: 0, data: {} });
  await again;
  assert.equal(problems[1], "读不到这一场的记录。");
  const third = feed.reload();
  answers.answer(2, meeting("r1", "ok", lines(1)));
  assert.equal(await third, true);
  assert.deepEqual([problems[2], shown.seqs()], [null, [1]]);
});

test("a room that was left takes nothing more", async () => {
  const { live, room: shown, server: answers, feed, opened, clock: timer } = fed();
  answers.answer(0, meeting("r1", "running", lines(1)));
  await opened;
  hearRun(live, about("r1", "running", "2026-11-16", true));
  hearEvent(live, said(1));
  hearEvent(live, said(2));
  feed.hear(said(2));
  const late = feed.reload();
  feed.stop();
  assert.equal(timer.due(), 0);
  answers.answer(1, meeting("r1", "ok", lines(1, 2, 3)));
  assert.equal(await late, false);
  assert.deepEqual([shown.status(), shown.seqs()], ["running", [1, 2]]);
});
