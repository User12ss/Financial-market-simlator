// A chart of the market screen, enlarged for as long as the pointer stays on it. The screen in the room is small,
// and so are its candles: resting the pointer on its chart brings the same chart up large, over the page, and
// moving the pointer off the large chart, out onto the dark round it, puts it away again. This file is only the
// coming and going. What the large chart shows is drawn by the screen it came from (`draw`), from the same record.
//
// Where the pointer is, is worked out from the boxes on the page, not from which piece of page heard it: a
// browser does not always say that a pointer is on something which has just appeared under it.

import { el, clear } from "./dom.js";
import { t } from "./i18n.js";

export const REST_MS = 350;           // the pointer stays this long on the small chart: passing over it enlarges nothing
export const AWAY_MS = 180;           // and may slip off the large one this long before that is put away
const FIT_MS = 120;

const within = (box, at) => at.x >= box.left && at.x <= box.right && at.y >= box.top && at.y <= box.bottom;
const placeOf = (pointer) => (pointer && typeof pointer.clientX === "number" ? { x: pointer.clientX, y: pointer.clientY } : null);

/**
 * `draw(panel)` fills the large chart and returns the name of what it shows, or null when there is nothing to
 * show (it is then not put up). The screen calls `watch` with each small chart it draws, `redraw` when what it
 * shows has changed, and `forget` when it shows no such chart any more.
 */
export function chartZoom({ draw }) {
  const state = {
    open: false,
    onChart: false,             // the pointer has been on the large chart since it came up
    resting: null, leaving: null, fitting: null,
    held: false,                // put away with the pointer lying on the small chart: that is not a new rest on it
    at: null, small: null, opener: null,
  };
  const panel = el("div", { class: "zoom-panel" });
  const away = el("p", { class: "zoom-away", text: t("鼠标移到这片暗处，大图就收起。也可以按 Esc，或点一下暗处。") });
  // A dialog, but not one that shuts the page off: the page behind it goes on (a meeting being played keeps
  // playing, and the chart follows it), so it is not called modal.
  const node = el("div", { class: "zoom", role: "dialog" }, panel, away);

  const still = (name) => {
    window.clearTimeout(state[name]);
    state[name] = null;
  };
  const onKey = (event) => {
    if (event.key !== "Escape") return;
    if (event.preventDefault) event.preventDefault();
    close();
  };
  // When the window changes size the chart is drawn again to fit it, once the size has settled.
  const onResize = () => {
    still("fitting");
    state.fitting = window.setTimeout(() => {
      state.fitting = null;
      redraw();
    }, FIT_MS);
  };
  const keyed = () => typeof document.addEventListener === "function";
  const sized = () => typeof window.addEventListener === "function";

  /** The pointer moved, or left the window: stay while it is on the large chart, go once it has been and is not. */
  function follow(pointer, gone = false) {
    if (!state.open) return;
    state.at = placeOf(pointer) || state.at;
    if (!gone && state.at && within(panel.getBoundingClientRect(), state.at)) {
      state.onChart = true;
      still("leaving");
    } else if (state.onChart && state.leaving === null) {
      state.leaving = window.setTimeout(close, AWAY_MS);
    }
  }
  node.addEventListener("pointermove", (pointer) => follow(pointer));
  node.addEventListener("pointerleave", (pointer) => follow(pointer, true));
  node.addEventListener("click", (pointer) => {
    if (pointer.target === node) close();              // on the dark itself: the words on it let a click through
  });

  function redraw() {
    if (!state.open) return;
    const name = draw(clear(panel));
    if (name === null || name === undefined) {
      close();
      return;
    }
    node.setAttribute("aria-label", t`${name} 的 K 线图，放大`);
  }

  /** Put the large chart up. `pointer` is what brought it (none when it was its button): where it lies is kept. */
  function open(pointer = null, opener = null) {
    if (state.open) return;
    still("resting");
    state.open = true;
    state.at = placeOf(pointer);
    state.opener = opener;
    state.held = false;
    document.body.append(node);
    if (keyed()) document.addEventListener("keydown", onKey);
    if (sized()) window.addEventListener("resize", onResize);
    redraw();
    if (!state.open) return;
    // A pointer that brought it up from under where it now stands is on it already.
    state.onChart = Boolean(state.at && within(panel.getBoundingClientRect(), state.at));
    // Brought up from the keyboard, the keys go to it: to the button that puts it away.
    const first = opener ? panel.querySelector("button.zoom-close") : null;
    if (first && typeof first.focus === "function") first.focus();
  }

  function close() {
    if (!state.open) return;
    still("leaving");
    still("fitting");
    state.open = false;
    state.onChart = false;
    if (keyed()) document.removeEventListener("keydown", onKey);
    if (sized()) window.removeEventListener("resize", onResize);
    document.body.removeChild(node);
    clear(panel);
    state.held = Boolean(state.small && state.at && within(state.small.getBoundingClientRect(), state.at));
    const back = state.opener;
    state.opener = null;
    if (back && typeof back.focus === "function") back.focus();
  }

  /** The small chart the screen has just drawn: resting the pointer on it brings the large one up. */
  function watch(small) {
    still("resting");
    state.small = small;
    let last = null;                                   // where on the small chart the pointer was last heard of
    // The wait starts when the pointer moves on the chart, not when the chart finds a pointer on it: a browser
    // says the second also of a chart that has just been drawn under a pointer lying still (the screen is drawn
    // again as a meeting plays), and a pointer left lying in the middle of the room is not a reader pointing.
    small.addEventListener("pointermove", (pointer) => {
      if (pointer.pointerType === "touch" || state.open || state.held) return;         // a finger does not hover
      last = pointer;
      if (state.resting !== null) return;
      state.resting = window.setTimeout(() => {
        state.resting = null;
        open(last);
      }, REST_MS);
    });
    small.addEventListener("pointerleave", () => {
      still("resting");
      state.held = false;
    });
  }

  /** The screen shows no chart to enlarge any more. */
  function forget() {
    still("resting");
    state.small = null;
    close();
  }

  return { node, watch, forget, redraw, open, close, isOpen: () => state.open };
}
