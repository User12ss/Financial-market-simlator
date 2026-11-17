// The meeting room's rules for what is on show: run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  advance, current, extend, isLive, load, newPlayback, pause, setFollow, show, start, wait,
} from "../../dashboard/static/js/playback.js";

const line = (seq, remark = "A line.") => ({ seq, remark });
const lines = (...seqs) => seqs.map((seq) => line(seq));
const meeting = (runId, live, events) => ({ session: "2026-11-16", run: { run_id: runId, live }, events });
const seqs = (state) => state.events.map((event) => event.seq);

test("an empty room shows nothing and cannot be played", () => {
  const state = newPlayback();
  assert.equal(current(state), null);
  assert.equal(isLive(state), false);
  assert.equal(start(state), false);
  assert.equal(state.playing, false);
  load(state, { session: null, run: null, events: [] });
  assert.deepEqual([state.index, state.follow, current(state)], [-1, false, null]);
  assert.equal(extend(state, "r1", lines(1)), 0);
});

test("a finished session opens on its first line and a run still going on its newest, followed", () => {
  const done = newPlayback();
  assert.equal(load(done, meeting("r1", false, lines(1, 2, 3))), false);
  assert.deepEqual([done.index, done.follow, current(done).seq], [0, false, 1]);

  const going = newPlayback();
  load(going, meeting("r2", true, lines(1, 2, 3)));
  assert.deepEqual([going.index, going.follow, current(going).seq, isLive(going)], [2, true, 3, true]);

  const justStarted = newPlayback();
  load(justStarted, meeting("r3", true, []));
  assert.deepEqual([justStarted.index, justStarted.follow, current(justStarted)], [-1, true, null]);
});

test("the same run loaded again keeps the reader's place, a different run does not", () => {
  const state = newPlayback();
  load(state, meeting("r1", false, lines(1, 2, 3, 4)));
  show(state, 2, true);
  state.playing = true;
  assert.equal(load(state, meeting("r1", false, lines(1, 2, 3, 4, 5))), true);
  assert.deepEqual([state.index, state.playing, seqs(state)], [2, true, [1, 2, 3, 4, 5]]);
  // The record handed over is copied, so adding to the room never changes what the page was given.
  const given = meeting("r1", false, lines(1, 2));
  load(state, given);
  assert.equal(state.index, 1);                       // it was on the third line, which is no longer there
  extend(state, "r1", lines(3));
  assert.equal(given.events.length, 2);

  assert.equal(load(state, meeting("r9", false, lines(1, 2, 3))), false);
  assert.deepEqual([state.index, state.playing, state.follow], [0, false, false]);
});

test("a followed run that is loaded again stays on its newest line", () => {
  const state = newPlayback();
  load(state, meeting("r1", true, lines(1, 2)));
  load(state, meeting("r1", true, lines(1, 2, 3, 4)));
  assert.deepEqual([state.index, state.follow], [3, true]);
  // The run ends: the same run, no longer going. The place and the following are kept.
  load(state, meeting("r1", false, lines(1, 2, 3, 4, 5)));
  assert.deepEqual([state.index, isLive(state)], [4, false]);
});

test("new lines are added once, in order, and only for the run on show", () => {
  const state = newPlayback();
  load(state, meeting("r1", true, lines(1, 2)));
  assert.equal(extend(state, "another-run", lines(3, 4)), 0);
  assert.deepEqual(seqs(state), [1, 2]);

  // The stream hands over everything it has each time: what is already there is skipped.
  assert.equal(extend(state, "r1", lines(1, 2, 3)), 1);
  assert.equal(extend(state, "r1", lines(1, 2, 3)), 0);
  assert.equal(extend(state, "r1", lines(1, 2, 3, 4, 5)), 2);
  assert.deepEqual(seqs(state), [1, 2, 3, 4, 5]);
  assert.equal(state.index, 4);                       // followed, so the newest is on show

  // A line that arrives out of order is not put in behind the ones already shown.
  assert.equal(extend(state, "r1", [line(7), line(6), line(8)]), 2);
  assert.deepEqual(seqs(state), [1, 2, 3, 4, 5, 7, 8]);
});

test("new lines do not move a reader who is not following", () => {
  const state = newPlayback();
  load(state, meeting("r1", true, lines(1, 2, 3)));
  show(state, 0, true);
  assert.equal(state.follow, false);
  assert.equal(extend(state, "r1", lines(4, 5)), 2);
  assert.deepEqual([state.index, seqs(state)], [0, [1, 2, 3, 4, 5]]);
});

test("stepping stays inside the record, and by hand it stops the playing", () => {
  const state = newPlayback();
  load(state, meeting("r1", false, lines(1, 2, 3)));
  show(state, 99);
  assert.equal(state.index, 2);
  show(state, -99);
  assert.equal(state.index, -1);
  state.playing = true;
  show(state, 1);
  assert.equal(state.playing, true);                  // moved on by the clock, not by hand
  show(state, 2, true);
  assert.deepEqual([state.index, state.playing], [2, false]);
});

test("stepping back by hand stops following, stepping to the newest line does not", () => {
  const state = newPlayback();
  load(state, meeting("r1", true, lines(1, 2, 3)));
  show(state, 2, true);
  assert.equal(state.follow, true);
  show(state, 1, true);
  assert.equal(state.follow, false);
  show(state, 2, true);
  assert.equal(state.follow, false);                  // following again is asked for, never assumed
  setFollow(state, true);
  assert.deepEqual([state.follow, state.index], [true, 2]);
});

test("playing a finished session goes line by line and stops at the end", () => {
  const state = newPlayback();
  load(state, meeting("r1", false, lines(1, 2, 3)));
  assert.equal(advance(state), "ended");              // not playing: nothing moves
  assert.equal(state.index, 0);

  assert.equal(start(state), true);
  assert.deepEqual([advance(state), state.index], ["shown", 1]);
  assert.deepEqual([advance(state), state.index], ["shown", 2]);
  assert.deepEqual([advance(state), state.index, state.playing], ["ended", 2, false]);

  // Pressing play at the end plays it again from the top.
  start(state);
  assert.deepEqual([state.index, advance(state), state.index], [-1, "shown", 0]);
  pause(state);
  assert.deepEqual([advance(state), state.index], ["ended", 0]);
});

test("pressing play on a run that is being followed plays it from the top and stops the following", () => {
  const state = newPlayback();
  load(state, meeting("r1", true, lines(1, 2)));
  assert.deepEqual([state.follow, state.index], [true, 1]);
  assert.equal(start(state), true);
  assert.deepEqual([state.follow, state.playing, state.index], [false, true, -1]);
  // Otherwise the next line to arrive would pull the reader to the end in the middle of the playback.
  advance(state);
  extend(state, "r1", lines(3));
  assert.equal(state.index, 0);
});

test("playing a run still going waits at the end and goes on when the next line is said", () => {
  const state = newPlayback();
  load(state, meeting("r1", true, lines(1, 2)));
  show(state, 0, true);
  start(state);
  assert.deepEqual([advance(state), state.index], ["shown", 1]);
  assert.deepEqual([advance(state), state.index, state.playing], ["waiting", 1, true]);
  extend(state, "r1", lines(3));
  assert.deepEqual([advance(state), state.index], ["shown", 2]);

  setFollow(state, true);
  assert.deepEqual([state.playing, state.index], [false, 2]);
  setFollow(state, false);
  assert.deepEqual([state.follow, state.index], [false, 2]);
});

test("a longer line stays up longer, within bounds, and the speed shortens it", () => {
  assert.equal(wait(line(1, ""), 1), 1600);
  assert.equal(wait(null, 1), 1600);
  assert.equal(wait(line(1, "x".repeat(50)), 1), 1400 + 38 * 50);
  assert.equal(wait(line(1, "x".repeat(5000)), 1), 8000);
  assert.equal(wait(line(1, "x".repeat(50)), 2), (1400 + 38 * 50) / 2);
  assert.equal(wait(line(1, "x".repeat(50)), 0.5), (1400 + 38 * 50) * 2);
});
