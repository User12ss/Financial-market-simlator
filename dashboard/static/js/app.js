// The page's frame: which model configuration, which tab, and the one live
// connection that tells every page when something new has been recorded.

import { el, clear, getJSON, empty } from "./dom.js";
import { hearEvent, hearRun, newLive, reconnected } from "./live.js";
import { overviewPage, roomPage, bookPage, evaluationPage, replayPage, statusPage } from "./pages.js";

const TABS = [
  ["overview", "总览", overviewPage], ["room", "会议室", roomPage], ["book", "持仓和订单", bookPage],
  ["evaluation", "评估", evaluationPage], ["replay", "回放", replayPage], ["status", "运行状态", statusPage],
];
const REFRESH_MS = 20000;
const RETRY_MS = 5000;

const main = document.getElementById("main");
const tabs = document.getElementById("tabs");
const picker = document.getElementById("arm");
const liveChip = el("a", { class: "chip", href: "#room", hidden: true });
document.querySelector(".brand").append(liveChip);

const app = { roster: [], arm: null, page: null, source: null, connected: true, live: newLive() };

function remembered() {
  try { return window.sessionStorage.getItem("arm"); } catch (error) { return null; }
}
function remember(arm) {
  try { window.sessionStorage.setItem("arm", arm); } catch (error) { /* private window: just do without */ }
}

function route() {
  const [name, day, run] = window.location.hash.replace(/^#/, "").split("/");
  const found = TABS.find(([id]) => id === name) || TABS[0];
  // After the page's name: a session date, then perhaps one run of that day. Anything else is ignored,
  // and the page opens on the newest. The run's name is only ever sent to the server as a question.
  const param = /^\d{4}-\d{2}-\d{2}$/.test(day || "") ? day : null;
  let which = null;
  try {
    which = param && run ? decodeURIComponent(run) : null;
  } catch (error) {
    which = null;
  }
  return { id: found[0], build: found[2], param, run: which };
}

function paintTabs(active) {
  clear(tabs);
  for (const [id, title] of TABS) {
    tabs.append(el("button", {
      type: "button", "aria-current": id === active ? "page" : null, text: title,
      onclick: () => { window.location.hash = `#${id}`; },
    }));
  }
}

function paintLive() {
  const run = app.live.run;
  liveChip.hidden = app.connected && !(run && run.live);
  if (!app.connected) {
    // Without the connection nothing new can arrive, so the page must not look live.
    clear(liveChip).append(el("span", { class: "state state-warning", text: "和看板服务的连接断了，正在重连" }));
  } else if (run && run.live) {
    clear(liveChip).append(el("span", { class: "state state-live", text: `流程正在运行：${app.live.session}` }));
  }
}

function render() {
  const { id, build, param, run } = route();
  if (app.page && app.page.destroy) app.page.destroy();
  paintTabs(id);
  app.page = build({ arm: app.arm, roster: app.roster, param, run, live: () => app.live });
  clear(main).append(app.page.node);
}

function listen() {
  if (app.source) app.source.close();
  app.live = newLive();
  paintLive();
  app.source = new EventSource(`/api/stream?arm=${encodeURIComponent(app.arm)}`);
  app.source.addEventListener("run", (message) => {
    const told = JSON.parse(message.data);
    const changed = hearRun(app.live, told);
    paintLive();
    if (app.page && app.page.onStream) app.page.onStream(told);
    // A run starting or ending changes the numbers on the other pages too.
    if (changed && app.page && app.page.refresh) app.page.refresh();
  });
  app.source.addEventListener("event", (message) => {
    const told = JSON.parse(message.data);
    hearEvent(app.live, told);
    if (app.page && app.page.onStream) app.page.onStream(told);
  });
  app.source.addEventListener("open", () => {
    reconnected(app.live);                          // the server now tells everything from the start
    const back = !app.connected;
    app.connected = true;
    paintLive();
    if (back && app.page && app.page.refresh) app.page.refresh();     // catch up on what changed meanwhile
  });
  app.source.addEventListener("error", () => {      // the browser keeps trying to reconnect by itself
    app.connected = false;
    paintLive();
  });
}

async function start() {
  const arms = await getJSON("/api/arms");
  picker.parentElement.hidden = !(arms.ok && arms.data.arms.length > 0);
  if (!arms.ok || !arms.data.journal || arms.data.arms.length === 0) {
    paintTabs(null);
    clear(main).append(empty(
      arms.ok ? "还没有任何运行记录：每日流程还没有跑过。跑过第一次之后，这个页面会自己出现内容。" : arms.data.error,
      arms.ok ? el("br") : null,
      arms.ok ? el("code", { text: arms.data.path }) : null));
    window.setTimeout(start, RETRY_MS);
    return;
  }
  clear(picker);
  const wanted = remembered();
  app.arm = arms.data.arms.includes(wanted) ? wanted : arms.data.arms[0];
  for (const arm of arms.data.arms) picker.append(el("option", { value: arm, selected: arm === app.arm, text: arm }));
  listen();
  render();
}

picker.addEventListener("change", () => {
  app.arm = picker.value;
  remember(app.arm);
  listen();
  render();
});
window.addEventListener("hashchange", () => { if (app.arm) render(); });
window.setInterval(() => {
  if (!document.hidden && app.page && app.page.refresh) app.page.refresh();
}, REFRESH_MS);

(async () => {
  const roster = await getJSON("/api/roster");
  if (roster.ok) app.roster = roster.data.roster;
  start();
})();
