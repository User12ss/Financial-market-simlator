// A chart enlarged while the pointer stays on it: when it comes up, when it is put away, and that it does neither by
// accident. Run with `node --test`. No browser here: the pieces are stand-ins, and the clock is one the test winds.

import assert from "node:assert/strict";
import { test } from "node:test";

import { FakeNode, install, wearing, words } from "./fake_dom.mjs";

const { window } = install();
// Work put off for later is only kept here, until the test lets the time pass.
const timers = [];
window.setTimeout = (work, after) => timers.push({ work, after, on: true });
window.clearTimeout = (number) => { if (timers[number - 1]) timers[number - 1].on = false; };
const waiting = () => timers.filter((timer) => timer.on).map((timer) => timer.after);
function pass() {
  for (const timer of timers.filter((item) => item.on)) {
    timer.on = false;
    timer.work();
  }
}
const heard = {};
globalThis.document.addEventListener = (name, handler) => { heard[name] = handler; };
globalThis.document.removeEventListener = (name, handler) => { if (heard[name] === handler) delete heard[name]; };

const { chartZoom, AWAY_MS, REST_MS } = await import("../../dashboard/static/js/zoom.js");

const box = (left, top, right, bottom) => () => ({ left, top, right, bottom, width: right - left, height: bottom - top });
const shown = () => globalThis.document.body.children.filter((node) => node.attributes.class === "zoom");

/** A small chart at the left edge of a window, and the large one that takes its middle when it is up. */
function setUp() {
  const drawn = [];
  const zoom = chartZoom({
    draw: (panel) => {
      drawn.push(panel);
      panel.append(Object.assign(new FakeNode("p"), { textContent: `drawing ${drawn.length}` }));
      return "TMO";
    },
  });
  const small = new FakeNode("div");
  small.getBoundingClientRect = box(10, 300, 90, 400);
  zoom.watch(small);
  const panel = wearing(zoom.node, "zoom-panel")[0];
  panel.getBoundingClientRect = box(100, 50, 900, 700);
  return { zoom, small, panel, drawn };
}
// Three places for the pointer: on the large chart, on the dark round it, and on the dark where the small chart lies.
const onPanel = { clientX: 500, clientY: 400 }, onDark = { clientX: 950, clientY: 400 }, onSmall = { clientX: 50, clientY: 350 };

test("passing over the small chart enlarges nothing; resting on it does", () => {
  const { zoom, small, drawn } = setUp();
  assert.deepEqual([zoom.isOpen(), shown()], [false, []]);
  small.listeners.pointermove(onSmall);
  assert.deepEqual(waiting(), [REST_MS]);
  small.listeners.pointerleave(onSmall);                      // it only passed over
  assert.deepEqual(waiting(), []);
  pass();
  assert.equal(zoom.isOpen(), false);

  small.listeners.pointermove(onSmall);
  assert.equal(zoom.isOpen(), false);                         // not yet
  pass();
  assert.equal(zoom.isOpen(), true);
  assert.deepEqual(shown(), [zoom.node]);
  assert.equal(drawn.length, 1);
  assert.equal(words(drawn[0]), "drawing 1");
  assert.equal(zoom.node.attributes.role, "dialog");
  assert.equal(zoom.node.attributes["aria-label"], "TMO 的 K 线图，放大");
  // Said on the dark round it: where the pointer goes to put it away.
  assert.match(words(wearing(zoom.node, "zoom-away")[0]), /暗处.*收起.*Esc/);
  // Entering the small chart again while the large one is up starts nothing more.
  small.listeners.pointermove(onSmall);
  assert.deepEqual(waiting(), []);
  zoom.close();
});

test("a chart drawn under a pointer lying still enlarges nothing: the pointer has to move on it", () => {
  const { zoom, small } = setUp();
  // All a browser says of a chart drawn under a still pointer is that the pointer is on it (the screen is drawn
  // again and again as a meeting plays). Nothing here waits on that.
  assert.equal(small.listeners.pointerenter, undefined);
  assert.deepEqual(waiting(), []);
  small.listeners.pointermove(onSmall);
  small.listeners.pointermove(onSmall);
  assert.deepEqual(waiting(), [REST_MS]);                     // one wait, however much it moves about
  small.listeners.pointerleave(onSmall);
  assert.deepEqual([waiting(), zoom.isOpen()], [[], false]);
});

test("a finger does not hover: touching the small chart starts no wait", () => {
  const { zoom, small } = setUp();
  small.listeners.pointermove({ ...onSmall, pointerType: "touch" });
  assert.deepEqual(waiting(), []);
  assert.equal(zoom.isOpen(), false);
});

test("the large chart stays while the pointer is on it and is put away once it has gone out onto the dark", () => {
  const { zoom, small, panel } = setUp();
  small.listeners.pointermove(onPanel);
  pass();
  zoom.node.listeners.pointermove(onPanel);
  zoom.node.listeners.pointermove({ clientX: 890, clientY: 690 });
  assert.deepEqual([zoom.isOpen(), waiting()], [true, []]);
  zoom.node.listeners.pointermove(onDark);                     // out on the dark
  assert.deepEqual(waiting(), [AWAY_MS]);
  zoom.node.listeners.pointermove({ clientX: 960, clientY: 410 });
  assert.deepEqual(waiting(), [AWAY_MS]);                      // still out: the same wait, not another
  zoom.node.listeners.pointermove(onPanel);                    // a slip of the hand: back in time
  assert.deepEqual(waiting(), []);
  pass();
  assert.equal(zoom.isOpen(), true);
  zoom.node.listeners.pointermove(onDark);
  pass();
  assert.deepEqual([zoom.isOpen(), shown(), panel.children], [false, [], []]);
  assert.equal(heard.keydown, undefined);                      // and it no longer listens for Esc
});

test("leaving the window from the large chart puts it away too", () => {
  const { zoom, small } = setUp();
  small.listeners.pointermove(onPanel);
  pass();
  zoom.node.listeners.pointermove(onPanel);
  zoom.node.listeners.pointerleave({ clientX: 1200, clientY: 400 });
  assert.deepEqual(waiting(), [AWAY_MS]);
  pass();
  assert.equal(zoom.isOpen(), false);
});

test("a pointer that was never on the large chart does not put it away by moving: Esc, a click on the dark, or its button do", () => {
  const { zoom } = setUp();
  zoom.open();                                                 // by its button: no pointer brought it up
  assert.equal(zoom.isOpen(), true);
  zoom.node.listeners.pointermove(onDark);
  zoom.node.listeners.pointerleave(onDark);
  assert.deepEqual(waiting(), []);
  heard.keydown({ key: "a" });
  assert.equal(zoom.isOpen(), true);
  let stopped = 0;
  heard.keydown({ key: "Escape", preventDefault: () => { stopped += 1; } });
  assert.deepEqual([zoom.isOpen(), stopped], [false, 1]);

  zoom.open();
  zoom.node.listeners.click({ target: wearing(zoom.node, "zoom-panel")[0] });      // a click on the chart is not one on the dark
  assert.equal(zoom.isOpen(), true);
  zoom.node.listeners.click({ target: zoom.node });
  assert.equal(zoom.isOpen(), false);

  // Brought up where the pointer already lies off the large chart (a small chart at the window's edge): moving
  // there does not put it away at once; going on to the chart and off again does.
  const edge = setUp();
  edge.small.listeners.pointermove({ clientX: 20, clientY: 350 });
  pass();
  edge.zoom.node.listeners.pointermove({ clientX: 24, clientY: 352 });
  assert.deepEqual([edge.zoom.isOpen(), waiting()], [true, []]);
  edge.zoom.node.listeners.pointermove(onPanel);
  edge.zoom.node.listeners.pointermove({ clientX: 24, clientY: 352 });
  assert.deepEqual(waiting(), [AWAY_MS]);
  pass();
  assert.equal(edge.zoom.isOpen(), false);
});

test("put away with the pointer lying on the small chart, it does not come straight back", () => {
  const { zoom, small } = setUp();
  small.listeners.pointermove(onSmall);
  pass();
  zoom.node.listeners.pointermove(onPanel);
  zoom.node.listeners.pointermove(onSmall);                    // off the large chart, and over where the small one is
  pass();
  assert.equal(zoom.isOpen(), false);
  small.listeners.pointermove(onSmall);                        // the pointer moves on, still on the small chart
  assert.deepEqual(waiting(), []);
  small.listeners.pointerleave(onSmall);                       // only after leaving it
  small.listeners.pointermove(onSmall);                        // does coming back count as resting on it again
  assert.deepEqual(waiting(), [REST_MS]);
  pass();
  assert.equal(zoom.isOpen(), true);
  // Put away with the pointer elsewhere, the next rest on the small chart counts at once.
  zoom.node.listeners.pointermove(onPanel);
  zoom.node.listeners.pointermove(onDark);
  pass();
  small.listeners.pointermove(onSmall);
  assert.deepEqual(waiting(), [REST_MS]);
  small.listeners.pointerleave(onSmall);
});

test("it is drawn again only while it is up, a new small chart takes over the wait, and forgetting it puts it away", () => {
  const { zoom, small, drawn } = setUp();
  zoom.redraw();
  assert.equal(drawn.length, 0);
  small.listeners.pointermove(onSmall);
  const next = new FakeNode("div");
  zoom.watch(next);                                            // the screen was drawn again under the pointer
  assert.deepEqual(waiting(), []);
  pass();
  assert.equal(zoom.isOpen(), false);
  next.listeners.pointermove(onSmall);
  pass();
  assert.equal(drawn.length, 1);
  zoom.redraw();
  assert.deepEqual([drawn.length, words(drawn[1])], [2, "drawing 2"]);      // what was there is replaced, not added to
  zoom.forget();                                               // the screen shows no chart of a ticker any more
  assert.deepEqual([zoom.isOpen(), shown()], [false, []]);
  next.listeners.pointermove(onSmall);
  zoom.forget();
  assert.deepEqual(waiting(), []);

  // Nothing to draw (the ticker went from the screen meanwhile): it is not left up empty.
  const none = chartZoom({ draw: () => null });
  none.open();
  assert.deepEqual([none.isOpen(), shown()], [false, []]);
});
