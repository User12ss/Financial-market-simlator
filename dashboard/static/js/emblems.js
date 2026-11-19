// The mark of each figure's field, drawn by code as a few lines: a bank for the
// central-bank analyst, a yield curve for the bond analyst, scales for the
// judge, a shield for the risk officer. Decoration only: they carry no data.

import { svg } from "./dom.js";

// Line drawings on a 24 by 24 square.
const MARKS = {
  pie: ["M12 3.5 A8.5 8.5 0 1 0 20.5 12 H12 Z", "M14.8 2.6 A8.6 8.6 0 0 1 21.4 9.2 H14.8 Z"],
  scope: ["M17 10.5 A6.5 6.5 0 1 1 4 10.5 A6.5 6.5 0 1 1 17 10.5 Z", "M15.3 15.3 L21 21", "M7.4 12 L9.7 9.5 L11.6 11.2 L13.7 8.3"],
  bank: ["M3 9 L12 4 L21 9 Z", "M5.6 9 V17 M9.9 9 V17 M14.1 9 V17 M18.4 9 V17", "M3.5 19.6 H20.5"],
  bull: ["M4 17 L9.5 11.5 L13 15 L20 7", "M15 7 H20 V12"],
  bear: ["M4 7 L9.5 12.5 L13 9 L20 17", "M15 17 H20 V12"],
  ledger: ["M7 3.5 H17 A2 2 0 0 1 19 5.5 V18.5 A2 2 0 0 1 17 20.5 H7 A2 2 0 0 1 5 18.5 V5.5 A2 2 0 0 1 7 3.5 Z",
    "M8.5 8 H15.5 M8.5 12 H15.5 M8.5 16 H12.5"],
  candles: ["M7 4 V8 M7 15 V20 M5 8 H9 V15 H5 Z", "M12 3 V6 M12 12 V19 M10 6 H14 V12 H10 Z", "M17 6 V10 M17 17 V21 M15 10 H19 V17 H15 Z"],
  curve: ["M4 4 V20 H20", "M6.5 16.5 C9 9.5 13 7.6 19 7"],
  scales: ["M12 4 V20 M7.5 20 H16.5 M5 7.5 H19", "M5 7.5 L2.9 13 A2.3 2.3 0 0 0 7.1 13 Z", "M19 7.5 L16.9 13 A2.3 2.3 0 0 0 21.1 13 Z"],
  exchange: ["M4 9 H19 L15.5 5.5", "M20 15 H5 L8.5 18.5"],
  barrel: ["M12 3.5 C8.5 8.5 6 11.5 6 14.8 A6 6 0 0 0 18 14.8 C18 11.5 15.5 8.5 12 3.5 Z", "M9 15 A3 3 0 0 0 12 18"],
  pulse: ["M3 12 H7.5 L9.8 6.5 L13 17.5 L15.4 12 H21"],
  shield: ["M12 3 L19.5 6 V11.5 C19.5 16 16.4 19.3 12 21 C7.6 19.3 4.5 16 4.5 11.5 V6 Z", "M8.8 11.9 L11.2 14.3 L15.4 9.7"],
  ticket: ["M6 5 H18 A2 2 0 0 1 20 7 V17 A2 2 0 0 1 18 19 H6 A2 2 0 0 1 4 17 V7 A2 2 0 0 1 6 5 Z", "M4 10 H20", "M8 13.6 H12 M8 16.2 H15"],
};

const lines = (id) => (MARKS[id] || []).map((d) => svg("path", { d }));

/** A figure's mark as a small picture of its own, for beside its name. */
export function emblem(id) {
  return svg("svg", { class: "emblem", viewBox: "0 0 24 24", "aria-hidden": "true" }, lines(id));
}

/** The same mark as part of a larger drawing, its top left corner at (x, y) and `size` units wide. */
export function emblemAt(id, x, y, size) {
  return svg("g", { class: "emblem", transform: `translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${(size / 24).toFixed(3)})` }, lines(id));
}

export const EMBLEMS = Object.keys(MARKS);
