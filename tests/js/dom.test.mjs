// How recorded words reach the page: as text, whatever they contain. Run with `node --test`.
// There is no browser here, so the page's builder is given a stand-in that notes what is done to it.

import assert from "node:assert/strict";
import { test } from "node:test";

class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.textContent = "";
  }

  setAttribute(name, value) { this.attributes[name] = value; }

  addEventListener(name, handler) { this.listeners[name] = handler; }

  append(...nodes) { this.children.push(...nodes); }

  set innerHTML(value) { throw new Error("the page must never build markup from a string"); }

  set outerHTML(value) { throw new Error("the page must never build markup from a string"); }
}

class FakeText extends FakeNode {
  constructor(text) {
    super("#text");
    this.textContent = text;
  }
}

globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (space, tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeText(text),
};

const { el, svg, table } = await import("../../dashboard/static/js/dom.js");

const HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script> & "quotes"';

test("words given as text are kept as text, markup characters and all", () => {
  const node = el("div", { text: HOSTILE });
  assert.equal(node.textContent, HOSTILE);
  assert.deepEqual([node.children, node.attributes], [[], {}]);
});

test("words given as children become text nodes, never elements", () => {
  const node = el("p", {}, "said: ", HOSTILE, 42, null, false, [el("b", { text: "x" }), [HOSTILE]]);
  assert.deepEqual(node.children.map((child) => [child.tag, child.textContent]), [
    ["#text", "said: "], ["#text", HOSTILE], ["#text", "42"], ["b", "x"], ["#text", HOSTILE],
  ]);
});

test("a recorded value may be an attribute's value, and stays exactly that", () => {
  const node = el("dt", { title: HOSTILE, text: "label" });
  assert.deepEqual(node.attributes, { title: HOSTILE });
  const drawn = svg("text", { x: 3, class: "plate", text: HOSTILE });
  assert.deepEqual([drawn.attributes, drawn.textContent], [{ x: "3", class: "plate" }, HOSTILE]);
});

test("a handler has to be one of the page's own functions: a string is refused, not run", () => {
  const pressed = () => {};
  assert.equal(el("button", { onclick: pressed }).listeners.click, pressed);
  for (const key of ["onclick", "onerror", "onload", "onmouseover"]) {
    assert.throws(() => el("div", { [key]: "alert(1)" }), TypeError);
    assert.throws(() => el("div", { [key]: { handleEvent() {} } }), TypeError);
  }
  // No handler at all is fine: nothing is attached.
  assert.deepEqual(el("div", { onclick: null }).listeners, {});
});

test("attributes that are switched off are left out, and ones switched on carry no value", () => {
  const node = el("button", { hidden: false, disabled: true, "aria-current": null, title: undefined, type: "button" });
  assert.deepEqual(node.attributes, { disabled: "", type: "button" });
});

test("a table puts each recorded cell in as text, and says so when there is nothing to list", () => {
  const columns = [
    { title: "标的", cell: (row) => row.ticker },
    { title: "理由（原文）", said: true, cell: (row) => row.reason },
    { title: "股数", num: true, cell: (row) => row.shares },
  ];
  const wrapper = table(columns, [{ ticker: "AAA", reason: HOSTILE, shares: null }]);
  const [head, body] = wrapper.children[0].children;
  assert.deepEqual(head.children[0].children.map((cell) => cell.textContent), ["标的", "理由（原文）", "股数"]);
  const cells = body.children[0].children;
  assert.deepEqual(cells.map((cell) => cell.children.map((child) => [child.tag, child.textContent])), [
    [["#text", "AAA"]], [["#text", HOSTILE]], [["#text", "—"]],               // a missing figure is a dash
  ]);
  assert.deepEqual(cells.map((cell) => cell.attributes.class), [undefined, "said", "num"]);

  const none = table(columns, [], "现在没有持仓。");
  assert.deepEqual([none.tag, none.children[0].textContent], ["p", "现在没有持仓。"]);
});
