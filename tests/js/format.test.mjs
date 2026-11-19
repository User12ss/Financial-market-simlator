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
