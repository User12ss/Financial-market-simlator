// How the page writes numbers, where it seats people and where it puts a chart's ticks: run with `node --test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { ticks } from "../../dashboard/static/js/charts.js";
import { DASH, bytes, num, pct, rate, signedPct, signedUsd, span, usd, word } from "../../dashboard/static/js/dom.js";
import { seats } from "../../dashboard/static/js/figures.js";

test("money is written with its sign, its commas and two decimals", () => {
  assert.equal(usd(99267.78), "$99,267.78");
  assert.equal(usd(-574.8759), "−$574.88");
  assert.equal(usd(100000, 0), "$100,000");
  assert.equal(usd(1430.625, 4), "$1,430.6250");
  assert.equal(signedUsd(44.887), "+$44.89");
  assert.equal(signedUsd(-732.2204), "−$732.22");
});

test("zero carries no sign, so nothing reads as a gain or a loss that is not there", () => {
  assert.equal(signedUsd(0), "$0.00");
  assert.equal(signedPct(0), "0.00%");
  assert.equal(usd(0), "$0.00");
});

test("a figure that is missing is a dash, never a zero", () => {
  for (const write of [usd, signedUsd, pct, signedPct, rate, num, span, bytes]) {
    assert.equal(write(null), DASH);
    assert.equal(write(undefined), DASH);
  }
  assert.equal(usd(NaN), DASH);
  assert.equal(word("side", null), DASH);
  assert.equal(word("side", ""), DASH);
});

test("percentages keep their sign, and a rate kept as a fraction is shown as a percentage", () => {
  assert.equal(pct(0.734), "0.73%");
  assert.equal(pct(-2.65), "−2.65%");
  assert.equal(pct(50.06, 1), "50.1%");
  assert.equal(signedPct(0.45), "+0.45%");
  assert.equal(signedPct(-11.26), "−11.26%");
  assert.equal(rate(0.6667), "67%");
  assert.equal(rate(1), "100%");
  assert.equal(rate(0), "0%");
  assert.equal(rate(0.6667, 1), "66.7%");
  assert.equal(num(224847), "224,847");
  assert.equal(num(0.85, 2), "0.85");
});

test("a length of time and a size are written in the units a person would use", () => {
  assert.equal(span(0), "0 秒");
  assert.equal(span(59.4), "59 秒");
  assert.equal(span(60), "1 分 0 秒");
  assert.equal(span(697.56), "11 分 38 秒");
  assert.equal(span(5347.4), "1 小时 29 分");
  assert.equal(bytes(512), "512 B");
  assert.equal(bytes(802816), "784.0 KB");
  assert.equal(bytes(5 * 1024 * 1024), "5.0 MB");
});

test("fixed vocabulary is named in Chinese, and anything else is shown as it was recorded", () => {
  assert.equal(word("side", "buy"), "买入");
  assert.equal(word("side", "sell"), "卖出");
  assert.equal(word("kind", "stop"), "止损");
  assert.equal(word("direction", "bearish"), "看空");
  assert.equal(word("action", "trim"), "减仓");
  assert.equal(word("verdict", "reduced"), "削减");
  assert.equal(word("run", "superseded"), "已被重跑取代");
  assert.equal(word("run", "running"), "运行中");
  assert.equal(word("appetite", "risk_off"), "偏防守");
  assert.equal(word("ownership", "do_not_own"), "不持有");
  assert.equal(word("position", "held"), "已持有");
  assert.equal(word("run", "something new"), "something new");
  assert.equal(word("no such group", "buy"), "buy");
});

test("chart ticks are round numbers that cover the whole range", () => {
  assert.deepEqual(ticks(97352.88, 100000), [97000, 98000, 99000, 100000]);
  assert.deepEqual(ticks(0, 1), [0, 0.25, 0.5, 0.75, 1]);
  assert.deepEqual(ticks(-1205.87, -5.72, 5), [-1250, -1000, -750, -500, -250, 0]);
  for (const [low, high] of [[3, 3], [0, 0], [-7.3, 12.9], [99990, 100010], [0.001, 0.002]]) {
    const marks = ticks(low, high);
    assert.ok(marks[0] <= low && marks[marks.length - 1] >= high, `${low}..${high} gives ${marks}`);
    assert.ok(marks.length >= 2 && marks.length <= 8, `${low}..${high} gives ${marks.length} ticks`);
    const steps = marks.slice(1).map((mark, index) => mark - marks[index]);
    assert.ok(steps.every((step) => step > 0 && Math.abs(step - steps[0]) < 1e-9 * Math.max(1, Math.abs(high))));
  }
});

test("fourteen people stand evenly round the room, the first at the top, opposite seats face to face", () => {
  const room = { cx: 500, cy: 352, rx: 400, ry: 232 };
  const places = seats(14, room);
  assert.equal(places.length, 14);
  const near = (a, b, within = 1) => Math.abs(a - b) <= within;

  assert.ok(near(places[0].x, 500) && near(places[0].y, 352 - 232));            // seat 0: top
  assert.ok(near(places[7].x, 500) && near(places[7].y, 352 + 232));            // seat 7: bottom
  for (let seat = 0; seat < 7; seat += 1) {                                       // seat n and n + 7: through the centre
    assert.ok(near(places[seat].x + places[seat + 7].x, 1000) && near(places[seat].y + places[seat + 7].y, 704));
  }
  assert.ok(places[1].x > 500 && places[13].x < 500);                            // clockwise from the top

  // Evenly spaced along the edge: neighbours are about equally far apart, and never crowded together.
  const gaps = places.map((place, seat) => {
    const next = places[(seat + 1) % 14];
    return Math.hypot(next.x - place.x, next.y - place.y);
  });
  assert.ok(Math.min(...gaps) > 130 && Math.max(...gaps) - Math.min(...gaps) < 12, `gaps ${gaps.map(Math.round)}`);

  // Those at the back are drawn smaller. The three nearest the top and the three nearest the bottom have their
  // name under them; the eight to either side carry it beside them, on the outer side, clear of the table and
  // of the market screen that stands in its middle.
  assert.ok(places[0].scale < places[3].scale && places[3].scale < places[7].scale);
  assert.deepEqual(places.map((place) => place.side), [0, 0, 1, 1, 1, 1, 0, 0, 0, -1, -1, -1, -1, 0]);
});

// The meeting room says how much room one figure needs, and seats are then spaced by need, not evenly.
const ROOM = { cx: 500, cy: 352, rx: 400, ry: 232, person: { wide: 100, tall: 125 } };

test("seventeen figures fit in the room at full size: closer along the top and bottom, further apart at the two ends", () => {
  const places = seats(17, ROOM);
  const near = (a, b, within = 1) => Math.abs(a - b) <= within;
  assert.equal(places.length, 17);
  assert.ok(near(places[0].x, 500) && near(places[0].y, 352 - 232));              // seat 0: still the top

  // Seat n and seat 17 - n stand at the same height on the two sides of the room.
  for (let seat = 1; seat <= 8; seat += 1) {
    const [right, left] = [places[seat], places[17 - seat]];
    assert.ok(right.x > 500 && near(right.x + left.x, 1000, 2) && near(right.y, left.y, 2), `seat ${seat}`);
    assert.ok(near(right.scale, left.scale, 0.01) && right.side === -left.side, `seat ${seat}`);
  }
  // Nobody stands at the very front, where the reader's own desk is: the two nearest stand either side of it.
  assert.ok(places[8].y > 570 && places[9].y > 570 && places[8].x - places[9].x > 100);

  // Nobody is drawn smaller for it: the room holds seventeen at the size it held fourteen.
  const fourteen = seats(14, { cx: 500, cy: 352, rx: 400, ry: 232 });
  assert.ok(near(places[0].scale, fourteen[0].scale, 0.001) && near(places[8].scale, 1.24, 0.01));

  // A figure is a hundred units tall at scale 1, with a sign over its head. Where one stands over another, the
  // lower one's head is clear of the upper one's feet; side by side, there is room for a name under each.
  let over = 0;
  places.forEach((place, seat) => {
    const next = places[(seat + 1) % 17];
    const [upper, lower] = place.y <= next.y ? [place, next] : [next, place];
    const apart = Math.abs(next.x - place.x);
    if (apart < 45) {                                                              // one over the other
      over += 1;
      assert.ok(lower.y - upper.y - 100 * lower.scale >= 15, `seats ${seat} and ${seat + 1} stand on each other`);
    } else if (!place.side && !next.side && Math.abs(next.y - place.y) < 20) {     // side by side, names on one line
      assert.ok(apart >= 100, `seats ${seat} and ${seat + 1} are only ${apart.toFixed(0)} apart`);
    }
  });
  assert.equal(over, 2);                                                           // one pair at each end of the room

  // A name goes under its figure unless the next one towards the front stands there: only the two at each
  // end of the room carry theirs beside them, on the outer side.
  assert.deepEqual(places.map((place) => place.side), [0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, -1, -1, 0, 0]);
});

test("spaced evenly, seventeen would stand on each other at the two ends of the room", () => {
  // What the room's own spacing is for. Evenly round the same edge, the pair at each end overlap.
  const places = seats(17, { cx: 500, cy: 352, rx: 400, ry: 232 });
  const [upper, lower] = [places[4], places[5]];
  assert.ok(Math.abs(upper.x - lower.x) < 45 && lower.y - upper.y - 100 * lower.scale < 0);
});

test("more figures than the room holds at full size are all drawn smaller by the same share", () => {
  const [fits, crowded] = [seats(17, ROOM), seats(24, ROOM)];
  const share = crowded[0].scale / fits[0].scale;
  assert.ok(share > 0.7 && share < 0.85, `drawn at ${share.toFixed(3)}`);
  assert.ok(Math.abs(crowded[12].scale - 1.24 * share) < 0.01);                    // seat 12 of 24: the very front
  assert.ok(seats(18, ROOM)[0].scale === fits[0].scale);                           // eighteen still fit
});

test("a room of one or two is laid out like any other, and no number of people breaks the two sides' symmetry", () => {
  assert.deepEqual(seats(1, ROOM).map((place) => [Math.round(place.x), Math.round(place.y), place.side]), [[500, 120, 0]]);
  const two = seats(2, ROOM);
  assert.ok(Math.abs(two[1].x - 500) < 1 && Math.abs(two[1].y - 584) < 1 && two.every((place) => place.side === 0));
  for (const count of [7, 17, 24, 30]) {
    const places = seats(count, ROOM);
    // The one at the top, in the middle, has no outer side to carry a name on: it stays under, however crowded.
    assert.equal(places[0].side, 0, `${count} seats`);
    for (let seat = 1; seat < count; seat += 1) {
      assert.ok(places[seat].side === -places[count - seat].side || (places[seat].side === 0 && places[count - seat].side === 0),
        `${count} seats, seat ${seat}`);
    }
  }
});
