// Stand-ins for the pieces of a browser the page's builders use, for tests run by node where there is none.
// A node here only notes what is done to it, so a test can read back what a reader would have in front of them.

/** Whether a node answers to a simple selector: a tag, classes, or both ("button", ".tick", ".tick.now"). */
function answers(node, selector) {
  const [tag, ...classes] = selector.split(".");
  const worn = String(node.attributes.class || "").split(/\s+/).concat([...node.classes]);
  return (!tag || node.tag === tag) && classes.every((name) => worn.includes(name));
}

export class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.textContent = "";
    this.style = { setProperty: (name, value) => { this.style[name] = value; } };
    this.dataset = {};
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      toggle: (name, on) => (on ? this.classes.add(name) : this.classes.delete(name)),
      contains: (name) => this.classes.has(name),
    };
  }

  get hidden() { return "hidden" in this.attributes; }

  set hidden(on) {
    if (on) this.attributes.hidden = "";
    else delete this.attributes.hidden;
  }

  get firstChild() { return this.children[0] || null; }

  setAttribute(name, value) {
    this.attributes[name] = value;
    if (name.startsWith("data-")) this.dataset[name.slice(5)] = value;
  }

  removeAttribute(name) { delete this.attributes[name]; }

  addEventListener(name, handler) { this.listeners[name] = handler; }

  append(...nodes) {
    // A real page would write the word "null" or "undefined" here. Nothing of the page's own may do that.
    for (const node of nodes) {
      if (node === null || node === undefined) throw new TypeError(`append(${node}) would put the word "${node}" on the page`);
    }
    this.children.push(...nodes);
  }

  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }

  querySelectorAll(selector) { return all(this, (node) => answers(node, selector)).filter((node) => node !== this); }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  getBoundingClientRect() { return { width: 0, height: 0, left: 3, bottom: 4 }; }
}

/** Every node in a piece of page that passes `test`, in the order a reader comes to them. */
export function all(node, test) {
  return [...(test(node) ? [node] : []), ...node.children.flatMap((child) => all(child, test))];
}

/** The nodes of a piece of page that wear a class. */
export const wearing = (node, name) => all(node, (item) => answers(item, `.${name}`));

/** The words of a node and everything in it that is on show, in reading order. */
export function words(node) {
  if (node.hidden) return "";
  return node.textContent + node.children.map(words).join("");
}

/** Every word, label and marker anywhere in a piece of page, shown or not. */
export function everything(node) {
  return [node.textContent, ...Object.values(node.attributes), ...Object.values(node.dataset), ...node.children.flatMap(everything)]
    .filter(Boolean);
}

/** Put the stand-ins where the page's code looks for a browser. Returns the one tooltip and the window. */
export function install() {
  const tooltip = new FakeNode("div");
  tooltip.hidden = true;
  globalThis.Node = FakeNode;
  globalThis.document = {
    createElement: (tag) => new FakeNode(tag),
    createElementNS: (space, tag) => new FakeNode(tag),
    createTextNode: (text) => Object.assign(new FakeNode("#text"), { textContent: text }),
    getElementById: () => tooltip,
    body: new FakeNode("body"),
  };
  // Work a page asks to have done again and again is only noted, with whether it was called off: `intervals`.
  const intervals = [];
  globalThis.window = {
    innerWidth: 1000, innerHeight: 800, location: { hash: "" }, history: { replaceState: () => {} },
    setTimeout: () => 1, clearTimeout: () => {}, matchMedia: () => ({ matches: true }),      // a reader who wants no motion
    intervals,
    setInterval: (work, every) => intervals.push({ work, every, stopped: false }),
    clearInterval: (number) => { if (intervals[number - 1]) intervals[number - 1].stopped = true; },
  };
  return { tooltip, window: globalThis.window };
}
