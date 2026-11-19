// The people in the meeting room, drawn by code: standing, full-body, facing
// the viewer. They are simple made-up figures told apart by their clothes,
// hair and one accessory. None is modelled on a real person.

import { svg } from "./dom.js";
import { emblemAt } from "./emblems.js";

/**
 * Seats around an ellipse, evenly spaced along its edge (not by angle, which
 * would crowd the two ends), clockwise from the top. Figures further back are
 * drawn a little smaller.
 */
export function seats(count, { cx, cy, rx, ry }) {
  const steps = 3600;
  const along = [0];
  const at = (step) => {
    const angle = -Math.PI / 2 + (step / steps) * 2 * Math.PI;
    return [cx + rx * Math.cos(angle), cy + ry * Math.sin(angle)];
  };
  for (let step = 1; step <= steps; step += 1) {
    const [x0, y0] = at(step - 1);
    const [x1, y1] = at(step);
    along.push(along[step - 1] + Math.hypot(x1 - x0, y1 - y0));
  }
  const found = [];
  let step = 0;
  for (let seat = 0; seat < count; seat += 1) {
    const wanted = (seat / count) * along[steps];
    while (along[step] < wanted) step += 1;
    const [x, y] = at(step);
    // Away from the middle of the room a name goes beside its figure, on the outer side, not below it: at the two
    // ends people stand one above the other, and nearer the middle the space under them belongs to the table.
    const side = Math.abs(x - cx) > 0.6 * rx ? Math.sign(x - cx) : 0;
    found.push({ x, y, side, scale: 1.0 + 0.24 * ((y - (cy - ry)) / (2 * ry)) });
  }
  return found;
}

function hairBehind(style) {
  if (style === "bob") return svg("rect", { class: "hair", x: -12, y: -100.5, width: 24, height: 23, rx: 10 });
  if (style === "long") return svg("rect", { class: "hair", x: -12.5, y: -100.5, width: 25, height: 35, rx: 10 });
  return null;
}

function hairOnTop(style) {
  if (style === "cap") {
    return [
      svg("path", { class: "cap", d: "M-10.8,-91.5 A10.8,10.8 0 0 1 10.8,-91.5 Z" }),
      svg("rect", { class: "cap", x: -12.5, y: -92.8, width: 25, height: 2.8, rx: 1.4 }),
    ];
  }
  const fringe = style === "crop"
    ? "M-10.3,-91 A10.3,10.3 0 0 1 10.3,-91 Q0,-97.5 -10.3,-91 Z"
    : "M-10.5,-89 A10.5,10.5 0 0 1 10.5,-89 L8.6,-92.5 Q0,-96.5 -8.6,-92.5 Z";
  const parts = [svg("path", { class: "hair", d: fringe })];
  if (style === "bun") parts.push(svg("circle", { class: "hair", cx: 0, cy: -101.5, r: 4.2 }));
  return parts;
}

function accessory(kind) {
  if (kind === "glasses") {
    return [
      svg("circle", { class: "wire", cx: -3.9, cy: -88.6, r: 3 }),
      svg("circle", { class: "wire", cx: 3.9, cy: -88.6, r: 3 }),
      svg("path", { class: "wire", d: "M-0.9,-88.6 H0.9" }),
    ];
  }
  if (kind === "tie") {
    return [
      svg("path", { class: "shirt", d: "M-5,-78 L5,-78 L0,-66 Z" }),
      svg("path", { class: "trim", d: "M-1.6,-77 L1.6,-77 L2.4,-63 L0,-59.5 L-2.4,-63 Z" }),
    ];
  }
  if (kind === "headset") {
    return [
      svg("path", { class: "wire thick", d: "M-10.6,-90 A10.6,11.2 0 0 1 10.6,-90" }),
      svg("rect", { class: "trim", x: 8.6, y: -92, width: 3.4, height: 6.4, rx: 1.4 }),
      svg("path", { class: "wire", d: "M10.4,-86 Q9.5,-81 4.4,-81.4" }),
    ];
  }
  if (kind === "badge") {
    return [
      svg("path", { class: "wire", d: "M-4,-78 L6.5,-70.5 M4,-78 L6.5,-70.5" }),
      svg("rect", { class: "shirt", x: 3.6, y: -70.5, width: 6, height: 7.4, rx: 1 }),
    ];
  }
  if (kind === "scarf") {
    return [
      svg("rect", { class: "trim", x: -8, y: -80.5, width: 16, height: 5, rx: 2.5 }),
      svg("rect", { class: "trim", x: 2.6, y: -78.5, width: 4.2, height: 13, rx: 2 }),
    ];
  }
  return [];
}

/** The parts of a figure standing on the point (0, 0), about a hundred units tall. The arms are their own groups so they can move. */
function parts(persona) {
  const worn = persona.extra === "glasses" || persona.extra === "headset" ? "none" : persona.extra;
  const onFace = worn === "none" ? persona.extra : "none";
  return [
    hairBehind(persona.hair),
    svg("rect", { class: "legs", x: -9.2, y: -45, width: 8.2, height: 43, rx: 3 }),
    svg("rect", { class: "legs", x: 1, y: -45, width: 8.2, height: 43, rx: 3 }),
    svg("rect", { class: "shoe", x: -10.6, y: -4.2, width: 10, height: 4.2, rx: 2 }),
    svg("rect", { class: "shoe", x: 0.6, y: -4.2, width: 10, height: 4.2, rx: 2 }),
    svg("g", { class: "arm arm-left" },
      svg("rect", { class: "cloth", x: -20.4, y: -76.5, width: 6.6, height: 31, rx: 3.3 }),
      svg("circle", { class: "skin", cx: -17.1, cy: -44.4, r: 3.3 })),
    svg("g", { class: "arm arm-right" },
      svg("rect", { class: "cloth", x: 13.8, y: -76.5, width: 6.6, height: 31, rx: 3.3 }),
      svg("circle", { class: "skin", cx: 17.1, cy: -44.4, r: 3.3 })),
    svg("rect", { class: "cloth", x: -14.5, y: -78.5, width: 29, height: 39, rx: 8 }),
    svg("rect", { class: "shade", x: 6.5, y: -77, width: 8, height: 36, rx: 6 }),
    svg("rect", { class: "skin", x: -3.2, y: -82.5, width: 6.4, height: 7, rx: 2 }),
    accessory(worn),
    svg("circle", { class: "skin", cx: 0, cy: -89, r: 10 }),
    svg("circle", { class: "face", cx: -3.9, cy: -88.6, r: 1.15 }),
    svg("circle", { class: "face", cx: 3.9, cy: -88.6, r: 1.15 }),
    svg("path", { class: "mouth", d: "M-2.8,-84.2 Q0,-82.4 2.8,-84.2" }),
    svg("ellipse", { class: "mouth-open", cx: 0, cy: -83.6, rx: 2.3, ry: 1.7 }),
    hairOnTop(persona.hair),
    accessory(onFace),
  ];
}

/** One figure in its seat. The pose group is what moves when it speaks or listens; the scale stays on the body. */
export function drawFigure(persona, seat) {
  const kind = persona.kind === "rule" ? "规则" : "模型";
  const plate = seat.side
    ? { x: seat.side * (27 * seat.scale + 4), y: -52 * seat.scale, anchor: seat.side > 0 ? "start" : "end" }
    : { x: 0, y: 17, anchor: "middle" };
  // The mark of its field goes in front of its title. A title is about 13 units a character.
  const wide = persona.title.length * 13;
  const mark = { size: 13, x: plate.anchor === "middle" ? -wide / 2 - 9 : plate.anchor === "start" ? plate.x : plate.x - wide - 17 };
  if (plate.anchor !== "end") plate.x += plate.anchor === "start" ? 17 : 8;
  return svg("g", {
    class: `figure ${persona.kind}`, transform: `translate(${seat.x.toFixed(1)},${seat.y.toFixed(1)})`,
    style: `--cloth: var(--tone-${persona.tone}); --order: ${persona.seat}`, tabindex: 0, role: "button",
    "data-id": persona.id, "aria-label": `${persona.title}（${kind}）`,
  },
    svg("ellipse", { class: "ripple", cx: 0, cy: -1, rx: 31 * seat.scale, ry: 9.5 * seat.scale }),
    svg("ellipse", { class: "halo", cx: 0, cy: -1, rx: 31 * seat.scale, ry: 9.5 * seat.scale }),
    svg("ellipse", { class: "ground", cx: 0, cy: -1, rx: 21 * seat.scale, ry: 5.5 * seat.scale }),
    svg("g", { class: "pose" }, svg("g", { class: "body", transform: `scale(${seat.scale.toFixed(3)})` }, parts(persona))),
    persona.emblem ? emblemAt(persona.emblem, mark.x, plate.y - 11.5, mark.size) : null,
    svg("text", { class: "plate", x: plate.x, y: plate.y, "text-anchor": plate.anchor, text: persona.title }),
    svg("text", { class: "role", x: plate.x, y: plate.y + 14, "text-anchor": plate.anchor, text: `${kind} · ${persona.group}` }));
}

/** The same figure's head and shoulders, small, for beside its words. */
export function portrait(persona) {
  return svg("svg", { class: "portrait figure", viewBox: "-15 -104 30 34", style: `--cloth: var(--tone-${persona.tone})`, "aria-hidden": "true" },
    svg("g", { class: "body" }, parts(persona)));
}
