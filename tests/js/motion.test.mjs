// Movement must never change what is shown: words are revealed, not altered, and a
// figure always ends on its real value. Run with `node --test`. There is no browser
// here, so the page's pieces are stand-ins and the test turns the animation frames itself.

import assert from "node:assert/strict";
import { test } from "node:test";

class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.textContent = "";
    this.classes = new Set();
    this.classList = { add: (name) => this.classes.add(name), remove: (name) => this.classes.delete(name) };
  }

  get firstChild() { return this.children[0] || null; }

  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }

  setAttribute(name, value) { this.attributes[name] = value; }

  append(...nodes) { this.children.push(...nodes); }

  getBoundingClientRect() { return { width: 0, height: 0 }; }
}

const frames = [];
const settings = { calm: false, now: 0 };
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeNode(tag), createTextNode: (text) => Object.assign(new FakeNode("#text"), { textContent: text }) };
globalThis.window = {
  matchMedia: () => ({ matches: settings.calm }),
  requestAnimationFrame: (work) => frames.push(work),
  cancelAnimationFrame: (id) => { frames[id - 1] = null; },
  performance: { now: () => settings.now },
};

/** Turn one animation frame, `ms` later. Returns whether anything was waiting. */
function frame(ms = 16) {
  settings.now += ms;
  const waiting = frames.splice(0).filter(Boolean);
  waiting.forEach((work) => work(settings.now));
  return waiting.length > 0;
}

const { calm, countUp, replay, typeOut } = await import("../../dashboard/static/js/motion.js");

const SAID = 'TMO\'s P/E of 24.9 & <b>"growth"</b> — 很强 👍 over 2 lines.\nSecond line.';
const parts = (node) => {
  const [whole, seen] = node.children;
  const [typed, rest] = seen.children;
  return { whole: whole.textContent, typed: typed.textContent, rest: rest.textContent, hiddenFromReaders: seen.attributes["aria-hidden"] };
};

test("words are revealed bit by bit, and at every moment they are exactly the recorded words", () => {
  settings.calm = false;
  const node = new FakeNode("div");
  typeOut(node, SAID, 900);
  let steps = 0;
  let before = "";
  do {
    const now = parts(node);
    assert.equal(now.typed + now.rest, SAID);                 // nothing added, changed or left out
    assert.equal(now.whole, SAID);                            // a screen reader has the whole of it from the start
    assert.equal(now.hiddenFromReaders, "true");              // and does not hear it letter by letter
    assert.ok(now.typed.startsWith(before));                  // what was shown is never taken back
    before = now.typed;
    steps += 1;
  } while (frame());
  assert.deepEqual([parts(node).typed, parts(node).rest], [SAID, ""]);
  assert.ok(steps > 3 && steps < 80, `${steps} steps`);       // a few frames, not one and not for ever
});

test("no character is cut in half on the way", () => {
  settings.calm = false;
  const node = new FakeNode("div");
  typeOut(node, "👍👍👍👍👍👍", 16 * 6);
  const seen = [];
  do { seen.push(parts(node).typed); } while (frame());
  assert.deepEqual(seen, ["", "👍", "👍👍", "👍👍👍", "👍👍👍👍", "👍👍👍👍👍", "👍👍👍👍👍👍"]);

  // One character at a time through text that mixes plain letters with ones a computer stores in two halves.
  const mixed = new FakeNode("div");
  typeOut(mixed, "a👍b很c👍", 16 * 60);
  const steps = [];
  do { steps.push([parts(mixed).typed, parts(mixed).rest]); } while (frame());
  assert.deepEqual(steps.map(([typed]) => typed), ["", "a", "a👍", "a👍b", "a👍b很", "a👍b很c", "a👍b很c👍"]);
  assert.ok(steps.every(([typed, rest]) => typed.isWellFormed() && rest.isWellFormed()));
});

test("the rest can be shown at once, and then nothing more happens", () => {
  settings.calm = false;
  const node = new FakeNode("div");
  const finish = typeOut(node, SAID, 900);
  frame();
  assert.notEqual(parts(node).typed, SAID);
  finish();
  assert.deepEqual([parts(node).typed, parts(node).rest], [SAID, ""]);
  assert.equal(frame(), false);
  finish();                                                   // asking twice does no harm
  assert.equal(parts(node).typed, SAID);
});

test("a reader who asked for less motion gets the words whole, at once", () => {
  settings.calm = true;
  assert.equal(calm(), true);
  const node = new FakeNode("div");
  typeOut(node, SAID, 900);
  assert.deepEqual([parts(node).typed, parts(node).rest, parts(node).whole], [SAID, "", SAID]);
  assert.equal(frame(), false);
  settings.calm = false;
  assert.equal(calm(), false);
  // Nothing to say, or no time to say it in: also at once.
  const empty = new FakeNode("div");
  typeOut(empty, "", 900);
  assert.deepEqual([parts(empty).typed, parts(empty).whole], ["", ""]);
  const hurried = new FakeNode("div");
  typeOut(hurried, SAID, 0);
  assert.equal(parts(hurried).typed, SAID);
});

test("a figure that runs up always ends on exactly its value", () => {
  settings.calm = false;
  const write = (value) => (value === null ? "—" : value.toFixed(2));
  const node = new FakeNode("span");
  countUp(node, 99267.78, write, 700);
  assert.equal(node.textContent, "0.00");
  const seen = [];
  while (frame(100)) seen.push(Number(node.textContent));
  assert.equal(node.textContent, "99267.78");
  assert.ok(seen.every((value, at) => at === 0 || value >= seen[at - 1]), `${seen}`);       // never past it and back
  assert.ok(Math.max(...seen) === 99267.78 && seen.length >= 7);

  const down = new FakeNode("span");
  countUp(down, -0.73, write, 300);
  while (frame(100));
  assert.equal(down.textContent, "-0.73");
});

test("a figure that is missing, zero, or shown to a reader who wants less motion is written once", () => {
  const write = (value) => (value === null || value === undefined ? "—" : String(value));
  for (const value of [null, undefined, 0, NaN]) {
    const node = new FakeNode("span");
    countUp(node, value, write);
    assert.equal(node.textContent, write(value));
    assert.equal(frame(), false);
  }
  settings.calm = true;
  const node = new FakeNode("span");
  countUp(node, 42, write);
  assert.equal(node.textContent, "42");
  assert.equal(frame(), false);
  settings.calm = false;
});

test("an entrance can be started over", () => {
  const node = new FakeNode("div");
  replay(node, "pop");
  assert.deepEqual([...node.classes], ["pop"]);
  replay(node, "pop");
  assert.deepEqual([...node.classes], ["pop"]);
});
