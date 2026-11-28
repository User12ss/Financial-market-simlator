// The people in the meeting room, drawn by code: standing, full-body, facing
// the viewer. They are simple made-up figures told apart by their clothes,
// hair and one accessory. None is modelled on a real person.

import { svg } from "./dom.js";
import { emblemAt } from "./emblems.js";
import { t } from "./i18n.js";

// A name under a figure takes this much below its feet: two lines, the title and what kind of seat it is.
export const NAME = 34;

/**
 * Seats around an ellipse, clockwise from the top. Figures further back are
 * drawn a little smaller.
 *
 * With nothing more said, they are evenly spaced along the edge (not by angle,
 * which would crowd the two ends). That suits marks on a map.
 *
 * A room of figures says how much room one `person` needs: `wide` sideways,
 * with its name under it, and `tall` up and down, with the sign over its head
 * (both for a figure at the back). A figure is taller than it is wide, so
 * seats are then spaced by need: closer along the top and the bottom, where
 * neighbours stand side by side, and further apart at the two ends, where one
 * stands above the other. Should there be more people than the room holds at
 * full size, the figures are all drawn smaller by the same share. Their names
 * are not, and would then run into each other: a room that full needs to be a
 * bigger room.
 */
export function seats(count, { cx, cy, rx, ry, person = null }) {
  const steps = 3600;
  const along = [0];
  const at = (step) => {
    const angle = -Math.PI / 2 + (step / steps) * 2 * Math.PI;
    return [cx + rx * Math.cos(angle), cy + ry * Math.sin(angle)];
  };
  const depth = (y) => 1.0 + 0.24 * ((y - (cy - ry)) / (2 * ry));
  for (let step = 1; step <= steps; step += 1) {
    const [x0, y0] = at(step - 1);
    const [x1, y1] = at(step);
    // What a step along the edge is worth: its length, or the share of one person's room it gives.
    along.push(along[step - 1] + (person
      ? Math.hypot((x1 - x0) / person.wide, (y1 - y0) / (person.tall * depth((y0 + y1) / 2)))
      : Math.hypot(x1 - x0, y1 - y0)));
  }
  const crowd = person ? Math.min(1, along[steps] / count) : 1;
  const found = [];
  let step = 0;
  for (let seat = 0; seat < count; seat += 1) {
    const wanted = (seat / count) * along[steps];
    while (along[step] < wanted) step += 1;
    const [x, y] = at(step);
    // Away from the middle of the map a name goes beside its mark, on the outer side, not below it.
    const side = !person && Math.abs(x - cx) > 0.6 * rx ? Math.sign(x - cx) : 0;
    found.push({ x, y, side, scale: depth(y) * crowd });
  }
  if (person) {
    // In a room a name goes under its figure, unless a neighbour nearer the front stands where it would be: its
    // head, and the sign over it, reach up past this one's feet. Then it goes beside the figure, on the outer side.
    const inTheWay = (place, other) => other.y > place.y + 1 && Math.abs(other.x - place.x) < 0.9 * person.wide
      && other.y - person.tall * other.scale < place.y + NAME;
    found.forEach((place, seat) => {
      const beside = [found[(seat + 1) % count], found[(seat + count - 1) % count]];
      // In the middle there is no outer side, so there the name stays under its figure even with a neighbour in
      // the way. That only happens in a room fuller than this one holds at full size.
      if (count > 1 && beside.some((other) => inTheWay(place, other))) place.side = Math.sign(Math.round(place.x - cx));
    });
  }
  return found;
}

// A figure is about a hundred units tall and stands on the point (0, 0). The head is small against the body, as on a
// grown person; the suit is dark and takes only a tint of the seat's colour, which is kept for the one accent each
// figure wears (a tie, a pocket square, a lanyard) and for its lines in the room. There is no face: these are
// positions, not portraits, and a drawn smile says something about a model's line that the record does not.

function hairBehind(style) {
  if (style === "bob") return svg("path", { class: "hair", d: "M-9.4,-93 A9.4,9.6 0 0 1 9.4,-93 L9.4,-84.5 Q9.4,-81.5 6.4,-81.5 L-6.4,-81.5 Q-9.4,-81.5 -9.4,-84.5 Z" });
  if (style === "long") return svg("path", { class: "hair", d: "M-9.6,-93 A9.6,9.8 0 0 1 9.6,-93 L10.4,-73.5 Q5,-71.5 0,-72 Q-5,-71.5 -10.4,-73.5 Z" });
  return null;
}

function hairOnTop(style) {
  const crown = style === "crop"
    ? "M-7.7,-93.4 A7.7,8.5 0 0 1 7.7,-93.4 Q3.8,-98.6 0,-98.2 Q-3.8,-98.6 -7.7,-93.4 Z"
    : style === "cap"
      ? "M-7.9,-92.2 A7.9,8.9 0 0 1 7.9,-92.2 L7.4,-95 Q0,-99.6 -7.4,-95 Z"
      : "M-8,-91.2 A8,9 0 0 1 8,-91.2 L7,-95.2 Q2.5,-97.4 -2,-96.2 Q-5.5,-95.4 -7,-93 Z";
  const parts = [svg("path", { class: "hair", d: crown })];
  if (style === "bun") parts.push(svg("circle", { class: "hair", cx: 0, cy: -101.4, r: 3.5 }));
  return parts;
}

function accessory(kind) {
  if (kind === "glasses") {
    return [
      svg("rect", { class: "wire", x: -6.1, y: -92.4, width: 5, height: 3.6, rx: 1.4 }),
      svg("rect", { class: "wire", x: 1.1, y: -92.4, width: 5, height: 3.6, rx: 1.4 }),
      svg("path", { class: "wire", d: "M-1.1,-90.9 H1.1" }),
    ];
  }
  if (kind === "headset") {
    return [
      svg("path", { class: "wire thick", d: "M-8.2,-91 A8.2,9.2 0 0 1 8.2,-91" }),
      svg("rect", { class: "trim", x: 6.6, y: -93, width: 3, height: 5.6, rx: 1.3 }),
      svg("path", { class: "wire", d: "M8.2,-87.6 Q7.6,-83.6 3.4,-84" }),
    ];
  }
  if (kind === "badge") {
    return [
      svg("path", { class: "lanyard", d: "M-3.6,-79.5 L3.4,-66 M3.6,-79.5 L3.4,-66" }),
      svg("rect", { class: "shirt", x: 0.8, y: -66.5, width: 5.4, height: 6.8, rx: 0.9 }),
      svg("rect", { class: "accent", x: 0.8, y: -66.5, width: 5.4, height: 1.9, rx: 0.9 }),
    ];
  }
  if (kind === "scarf") {            // the judge: a dark stole over the jacket, edged in the seat's colour
    return [
      svg("path", { class: "stole", d: "M-6.4,-80 L-3.2,-80 L-2.2,-50 L-6.6,-50 Z" }),
      svg("path", { class: "stole", d: "M6.4,-80 L3.2,-80 L2.2,-50 L6.6,-50 Z" }),
      svg("path", { class: "accent", d: "M-6.6,-51.6 H-2.2 V-50 H-6.6 Z M2.2,-51.6 H6.6 V-50 H2.2 Z" }),
    ];
  }
  return [];
}

/** What a figure wears at the collar: a tie in its seat's colour, or an open collar and a pocket square in it. */
function collar(persona) {
  const shirt = svg("path", { class: "shirt", d: "M-4.4,-80.4 L4.4,-80.4 L0,-63.5 Z" });
  const lapels = svg("path", { class: "lapel", d: "M-4.4,-80.4 L0,-63.5 L4.4,-80.4" });
  const tie = persona.extra === "tie" || ["short", "crop", "cap"].includes(persona.hair);
  if (persona.extra === "badge" || persona.extra === "scarf") return [shirt, lapels];
  return tie
    ? [shirt, svg("path", { class: "accent", d: "M-1.3,-79.6 L1.3,-79.6 L1.9,-67.5 L0,-64.6 L-1.9,-67.5 Z" }), lapels]
    : [shirt, lapels, svg("path", { class: "accent", d: "M5.6,-72.4 L10,-72.9 L10,-70.9 L5.6,-70.6 Z" })];
}

/** The parts of a figure standing on the point (0, 0), about a hundred units tall. The arms are their own groups so they can move. */
function parts(persona) {
  const onFace = persona.extra === "glasses" || persona.extra === "headset" ? persona.extra : "none";
  const worn = onFace === "none" ? persona.extra : "none";
  return [
    hairBehind(persona.hair),
    svg("path", { class: "legs", d: "M-8.8,-45 L-0.6,-45 L-1.2,-3 L-8,-3 Z" }),
    svg("path", { class: "legs", d: "M0.6,-45 L8.8,-45 L8,-3 L1.2,-3 Z" }),
    svg("path", { class: "shoe", d: "M-9.6,0 L-9.6,-2.2 Q-9.6,-4 -7.6,-4 L-0.9,-4 L-0.9,0 Z" }),
    svg("path", { class: "shoe", d: "M9.6,0 L9.6,-2.2 Q9.6,-4 7.6,-4 L0.9,-4 L0.9,0 Z" }),
    svg("g", { class: "arm arm-left" },
      svg("path", { class: "cloth", d: "M-14.2,-78.6 Q-18.6,-77.6 -18.9,-72.5 L-19.5,-47.5 L-14.6,-47.5 L-13.6,-70 Z" }),
      svg("rect", { class: "shirt", x: -19.5, y: -47.9, width: 4.9, height: 1.5 }),
      svg("ellipse", { class: "skin", cx: -17, cy: -44.2, rx: 2.5, ry: 2.9 })),
    svg("g", { class: "arm arm-right" },
      svg("path", { class: "cloth", d: "M14.2,-78.6 Q18.6,-77.6 18.9,-72.5 L19.5,-47.5 L14.6,-47.5 L13.6,-70 Z" }),
      svg("rect", { class: "shirt", x: 14.6, y: -47.9, width: 4.9, height: 1.5 }),
      svg("ellipse", { class: "skin", cx: 17, cy: -44.2, rx: 2.5, ry: 2.9 })),
    // The jacket: square shoulders, a little narrower at the hem.
    svg("path", { class: "cloth", d: "M-14.6,-74.6 Q-14.6,-80.6 -8.6,-80.6 L8.6,-80.6 Q14.6,-80.6 14.6,-74.6 L12.4,-42.6 Q12.3,-41 10.6,-41 L-10.6,-41 Q-12.3,-41 -12.4,-42.6 Z" }),
    svg("path", { class: "shade", d: "M0,-63.5 L4.4,-80.6 L8.6,-80.6 Q14.6,-80.6 14.6,-74.6 L12.4,-42.6 Q12.3,-41 10.6,-41 L0,-41 Z" }),
    svg("rect", { class: "skin", x: -2.7, y: -84.6, width: 5.4, height: 6.2, rx: 1.6 }),
    collar(persona),
    svg("circle", { class: "button", cx: 0, cy: -56.5, r: 0.9 }),
    accessory(worn),
    svg("ellipse", { class: "skin", cx: 0, cy: -91, rx: 7.5, ry: 8.5 }),
    hairOnTop(persona.hair),
    accessory(onFace),
    // Said while speaking, in place of a moving mouth: three bars beside the head, as on a sound meter.
    svg("g", { class: "voice", "aria-hidden": "true" },
      svg("rect", { x: 11.5, y: -97, width: 1.7, height: 7, rx: 0.8 }),
      svg("rect", { x: 14.4, y: -99, width: 1.7, height: 11, rx: 0.8 }),
      svg("rect", { x: 17.3, y: -96, width: 1.7, height: 5, rx: 0.8 })),
  ];
}

/** One figure in its seat. The pose group is what moves when it speaks or listens; the scale stays on the body. */
export function drawFigure(persona, seat) {
  const kind = persona.kind === "rule" ? t("规则") : t("模型");
  // Beside a figure the name stands at its chest, high enough that its second line is clear of the sign over the
  // head of whoever stands next towards the front.
  const plate = seat.side
    ? { x: seat.side * (27 * seat.scale + 4), y: -57 * seat.scale, anchor: seat.side > 0 ? "start" : "end" }
    : { x: 0, y: 17, anchor: "middle" };
  // The mark of its field goes in front of its title. A Chinese character is about 13 units wide at this size;
  // a Latin letter, on the English page where names are set smaller, a little under 6 (measured: 5.8 on average).
  const wide = Array.from(persona.title).reduce((sum, letter) => sum + (letter.charCodeAt(0) > 255 ? 13 : 5.8), 0);
  const mark = { size: 13, x: plate.anchor === "middle" ? -wide / 2 - 9 : plate.anchor === "start" ? plate.x : plate.x - wide - 17 };
  // The second line starts under the mark where the name stands to the right of its figure: at the right-hand
  // end of the room there is no width to spare.
  const second = plate.anchor === "start" ? plate.x : plate.x + (plate.anchor === "middle" ? 8 : 0);
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
    svg("text", { class: "role", x: second, y: plate.y + 14, "text-anchor": plate.anchor, text: `${kind} · ${persona.group}` }));
}

/** The same figure's head and shoulders, small, for beside its words. */
export function portrait(persona) {
  return svg("svg", { class: "portrait figure", viewBox: "-15 -104 30 34", style: `--cloth: var(--tone-${persona.tone})`, "aria-hidden": "true" },
    svg("g", { class: "body" }, parts(persona)));
}
