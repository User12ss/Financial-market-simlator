// Movement on the page. Everything here first asks whether the reader wants
// less motion, and then does without. None of it changes what is shown: words
// are revealed, never altered, and a figure always ends on its real value.

import { el, clear } from "./dom.js";

/** Whether the reader has asked their system for less motion. */
export function calm() {
  return typeof window !== "undefined" && Boolean(window.matchMedia)
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Show recorded words a few characters at a time, as if being said.
 *
 * The words are exactly the ones given: the part not yet revealed is already
 * in place, only unseen, so the box never changes size and nothing is added
 * or left out. A screen reader is given the whole text at once, and never
 * hears it letter by letter. Returns a way to show the rest at once.
 */
export function typeOut(node, text, budget = 900) {
  const typed = el("span", {});
  const rest = el("span", { class: "untyped" });
  clear(node).append(
    el("span", { class: "sr-only", text }),
    el("span", { "aria-hidden": "true" }, typed, rest));
  const letters = Array.from(text);                   // whole characters, so none is cut in half
  let shown = 0;
  let frame = null;
  const put = () => {
    typed.textContent = letters.slice(0, shown).join("");
    rest.textContent = letters.slice(shown).join("");
  };
  const finish = () => {
    if (frame !== null) window.cancelAnimationFrame(frame);
    frame = null;
    shown = letters.length;
    put();
  };
  if (calm() || letters.length === 0 || budget <= 0) {
    finish();
    return finish;
  }
  const each = Math.max(1, Math.ceil(letters.length / (budget / 16)));
  const step = () => {
    shown = Math.min(letters.length, shown + each);
    put();
    frame = shown < letters.length ? window.requestAnimationFrame(step) : null;
  };
  put();
  frame = window.requestAnimationFrame(step);
  return finish;
}

/** Run a figure up to its value from nothing. It always ends on exactly the value given. */
export function countUp(node, value, write, duration = 700) {
  if (calm() || typeof value !== "number" || !Number.isFinite(value) || value === 0) {
    node.textContent = write(value);
    return;
  }
  const began = window.performance.now();
  const step = (now) => {
    const done = Math.min(1, (now - began) / duration);
    node.textContent = write(done < 1 ? value * (1 - (1 - done) ** 3) : value);
    if (done < 1) window.requestAnimationFrame(step);
  };
  node.textContent = write(0);
  window.requestAnimationFrame(step);
}

/** Start an element's entrance animation over, for when what it shows has changed. */
export function replay(node, name) {
  node.classList.remove(name);
  void node.getBoundingClientRect();                  // lets the browser notice the class went away
  node.classList.add(name);
}
