/**
 * Browser tests for the graph UI. Run against a live `wrangler pages dev`.
 *
 *   node memora-graph/scripts/test_ui.mjs [baseURL]      (default http://localhost:8788)
 *
 * Companion to test_lineage.mjs, which covers pure logic. Everything here needs a
 * real DOM: geometry, CSS custom properties, localStorage, and the render loop.
 * Each case maps to a defect that actually shipped — see the comment on each.
 *
 * NOTE: index.html is served at "/" and is a SYMLINK in the repo. `wrangler pages
 * dev` does not serve symlinked assets, so CI replaces it with a resolved copy
 * before starting the server. Deploys follow the symlink, so the bytes match.
 */
import { chromium } from "playwright";

const BASE = process.argv[2] || "http://localhost:8788";
const pass = [];
const fail = [];
const EXPECTED_CHECKS = 35;
const check = (name, ok, detail) =>
  (ok ? pass : fail).push(`${name}${detail ? ` — ${detail}` : ""}`);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 820 } });
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(String(e)));

// ---------------------------------------------------------------- index.html

// SHIPPED BUG: the database names were hardcoded in the <option> markup AND in a
// `?db=` whitelist, so a third configured database was invisible here while
// force-graph (which reads /api/databases) listed it.
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await page.waitForSelector("#db-select", { timeout: 30000 });
await page.waitForFunction(
  () => document.querySelectorAll("#db-select option").length > 0,
  null,
  { timeout: 30000 },
);

const configured = await (await fetch(`${BASE}/api/databases`)).json();
const expected = configured.databases;
const options = await page.$$eval("#db-select option", (os) => os.map((o) => o.value));
check(
  "index: selector lists every configured database",
  expected.every((d) => options.includes(d)) && options.length === expected.length,
  `api=${expected.join(",")} dom=${options.join(",")}`,
);

// A database that is NOT the default proves the URL parameter is not whitelisted.
const nonDefault = expected.find((d) => d !== configured.default) || expected[0];
await page.goto(`${BASE}/?db=${nonDefault}`, { waitUntil: "networkidle" });
await page.waitForSelector("#db-select", { timeout: 30000 });
await page.waitForFunction(
  (want) => document.getElementById("db-select")?.value === want,
  nonDefault,
  { timeout: 30000 },
).catch(() => {});
check(
  "index: ?db= honours any configured database",
  (await page.$eval("#db-select", (s) => s.value)) === nonDefault,
  `asked ${nonDefault}, got ${await page.$eval("#db-select", (s) => s.value)}`,
);

// ------------------------------------------------- index.html truncation banner

// Truncation probes use the default/seeded database. nonDefault (ob1) is a
// catalog-only binding here and /api/graph?db=ob1 is not a fixture JSON graph.
const seededDb = configured.default || "memora";

// Happy path (real fixture, truncated=false) must NOT sprout a banner.
await page.goto(`${BASE}/?db=${seededDb}`, { waitUntil: "networkidle" });
await page.waitForSelector("#truncation-banner", { state: "attached", timeout: 30000 });
check(
  "index: happy path (truncated=false) shows no truncation banner",
  await page.$eval("#truncation-banner", (n) => getComputedStyle(n).display === "none"),
);

// Inject truncated=true and drive the raise control through the real fetch path.
let injectTruncated = true;
const injectedTotal = 5000;
let injectedNodeCount = 0;
await page.route("**/api/graph*", async (route) => {
  const url = route.request().url();
  const path = new URL(url).pathname;
  if (path !== "/api/graph") {
    await route.continue();
    return;
  }
  const resp = await fetch(url);
  const text = await resp.text();
  if (!text.startsWith("{") && !text.startsWith("[")) {
    await route.continue();
    return;
  }
  const json = JSON.parse(text);
  injectedNodeCount = (json.nodes || []).length;
  json.truncated = injectTruncated;
  json.total = injectedTotal;
  await route.fulfill({ json, status: 200, contentType: "application/json" });
});

await page.goto(`${BASE}/?db=${seededDb}`, { waitUntil: "networkidle" });
await page.waitForSelector("#truncation-banner", { state: "visible", timeout: 30000 });
const indexBannerVisible = await page.$eval(
  "#truncation-banner",
  (n) => getComputedStyle(n).display === "flex",
);
const indexBannerText = await page.$eval("#truncation-banner-text", (n) => n.textContent);
check(
  "index: truncated banner visible with showing N of 5000 nodes",
  indexBannerVisible
    && indexBannerText === `showing ${injectedNodeCount} of ${injectedTotal} nodes`,
  `text="${indexBannerText}" count=${injectedNodeCount}`,
);

// Click raise -> the NEXT /api/graph request must carry ?limit=K (2000 < K <= 5000).
const [indexRaiseResp] = await Promise.all([
  page.waitForResponse((r) => r.url().includes("/api/graph")),
  page.click("#truncation-raise"),
]);
const indexRaiseUrl = indexRaiseResp.url();
const indexRaiseLimit = Number(new URL(indexRaiseUrl).searchParams.get("limit"));
check(
  "index: raise control sends ?limit=K with 2000 < K <= 5000",
  indexRaiseLimit > 2000 && indexRaiseLimit <= 5000,
  `url=${indexRaiseUrl} limit=${indexRaiseLimit}`,
);

// Second raise reaches the hard max (2000 → 3500 → 5000) while still truncated.
const [indexMaxResp] = await Promise.all([
  page.waitForResponse((r) => r.url().includes("/api/graph")),
  page.click("#truncation-raise"),
]);
const indexMaxLimit = Number(new URL(indexMaxResp.url()).searchParams.get("limit"));
const indexAtMax = await page.$eval("#truncation-raise", (n) => n.disabled);
const indexMaxLabel = await page.$eval("#truncation-raise", (n) => n.textContent);
check(
  "index: at hard max + truncated, banner stays and control disabled",
  indexMaxLimit === 5000
    && indexAtMax
    && /5000/.test(indexMaxLabel)
    && await page.$eval("#truncation-banner", (n) => getComputedStyle(n).display === "flex"),
  `limit=${indexMaxLimit} disabled=${indexAtMax} label="${indexMaxLabel}"`,
);

// Follow-up with truncated=false -> banner is gone.
injectTruncated = false;
const [indexClearResp] = await Promise.all([
  page.waitForResponse((r) => r.url().includes("/api/graph")),
  page.evaluate(() => window.reloadGraphData()),
]);
await page.waitForTimeout(300);
check(
  "index: follow-up truncated=false hides the banner",
  await page.$eval("#truncation-banner", (n) => getComputedStyle(n).display === "none"),
);
await page.unroute("**/api/graph*");

// ---------------------------------------------------------------- force-graph

await page.goto(`${BASE}/force-graph.html`, { waitUntil: "networkidle" });
await page.waitForFunction(() => !!window.MemoraDebug?.fg, null, { timeout: 45000 });
await page.waitForTimeout(2500);

// Happy path (real fixture, truncated=false) must NOT sprout a banner.
check(
  "force-graph: happy path (truncated=false) shows no truncation banner",
  await page.$eval("#truncation-banner", (n) => getComputedStyle(n).display === "none"),
);

// Open the detail drawer so BOTH drawers are open (timeline is open by default).
await page.click(".trow", { timeout: 15000 }).catch(() => {});
await page.waitForTimeout(1200);

const geo = await page.evaluate(() => {
  const rect = (sel) => {
    const n = document.querySelector(sel);
    if (!n) return null;
    const b = n.getBoundingClientRect();
    const style = getComputedStyle(n);
    return { left: Math.round(b.left), right: Math.round(b.right),
      width: Math.round(b.width), height: Math.round(b.height),
      visible: style.visibility !== "hidden" && style.display !== "none" && b.width > 0 && b.height > 0 };
  };
  return {
    classes: document.body.className,
    bar: rect("#bar"),
    timeline: rect("#timeline"),
    panel: rect("#panel"),
  };
});
const bothOpen = /timeline-open/.test(geo.classes) && /panel-open/.test(geo.classes);
check("force-graph: both drawers are visibly open",
  bothOpen && geo.timeline?.visible && geo.panel?.visible, geo.classes.trim());

// SHIPPED BUG: the drawers are fixed, full-height and stack ABOVE #bar (z-index
// 31/32 vs 30), but the bar was sized to the whole viewport — with both drawers
// open its right-hand controls were painted over and unreachable.
const drawerLeft = Math.min(geo.timeline?.left ?? -1, geo.panel?.left ?? -1);
check("force-graph: top bar stays visible and clear of open drawers",
  !!geo.bar?.visible && drawerLeft > 0 && geo.bar.right <= drawerLeft,
  JSON.stringify({ bar: geo.bar, drawerLeft }));

const rect = (sel) => page.$eval(sel, (n) => {
  const b = n.getBoundingClientRect(); return { x: b.x, width: b.width };
});
const assertActionableHandle = async (sel) => {
  const state = await page.$eval(sel, (n) => {
    const b = n.getBoundingClientRect();
    const style = getComputedStyle(n);
    return { width: b.width, height: b.height, x: b.x, y: b.y,
      visible: style.display !== "none" && style.visibility !== "hidden",
      viewport: b.x >= 0 && b.y >= 0 && b.right <= innerWidth && b.bottom <= innerHeight };
  });
  if (!state.visible || state.width <= 0 || state.height <= 0 || !state.viewport) {
    throw new Error("resize handle is not actionable: " + sel + " " + JSON.stringify(state));
  }
};
const drag = async (sel, targetX) => {
  const b = await rect(sel);
  await page.mouse.move(b.x + b.width / 2, 200);
  await page.mouse.down();
  await page.mouse.move(targetX, 200, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(350);
};
await page.setViewportSize({ width: 1280, height: 820 });
await drag("#timeline-resize-handle", 1280 - 420);
const timeline420 = await rect("#timeline");
await drag("#detail-resize-handle", 1280 - 420 - 640);
const detail640 = await rect("#panel");
check("force-graph: real handles resize drawers independently",
  Math.abs(timeline420.width - 420) <= 2 && Math.abs(detail640.width - 640) <= 2,
  JSON.stringify({ timeline: timeline420.width, detail: detail640.width }));
await drag("#timeline-resize-handle", 1279);
const timelineMin = await rect("#timeline");
await drag("#timeline-resize-handle", -10);
const timelineMax = await rect("#timeline");
check("force-graph: timeline drag enforces 280px and 90vw clamps",
  Math.abs(timelineMin.width - 280) <= 2 && Math.abs(timelineMax.width - 1152) <= 2,
  JSON.stringify({ min: timelineMin.width, max: timelineMax.width }));
await page.setViewportSize({ width: 600, height: 820 });
await page.waitForTimeout(350);
const timelineReclamped = await rect("#timeline");
check("force-graph: viewport resize re-clamps drawer width",
  Math.abs(timelineReclamped.width - 540) <= 2, String(timelineReclamped.width));
await page.setViewportSize({ width: 1280, height: 820 });
await page.waitForTimeout(350);
await assertActionableHandle("#timeline-resize-handle");
await page.dblclick("#timeline-resize-handle");
await assertActionableHandle("#detail-resize-handle");
await page.dblclick("#detail-resize-handle");
await page.waitForTimeout(500);
const resetTimeline = await rect("#timeline");
const resetDetail = await rect("#panel");
const canvasGeometry = await page.evaluate(() => {
  const graph = document.querySelector("#graph").getBoundingClientRect();
  const canvas = document.querySelector("#graph canvas")?.getBoundingClientRect();
  return { graph: Math.round(graph.width), canvas: canvas ? Math.round(canvas.width) : 0 };
});
check("force-graph: double-click resets both drawer defaults",
  Math.abs(resetTimeline.width - 360) <= 2 && Math.abs(resetDetail.width - 380) <= 2,
  JSON.stringify({ timeline: resetTimeline.width, detail: resetDetail.width }));
check("force-graph: relayout resizes canvas with the graph area",
  canvasGeometry.canvas > 0 && Math.abs(canvasGeometry.graph - canvasGeometry.canvas) <= 2,
  JSON.stringify(canvasGeometry));

// SHIPPED BUG: the 3D render loop repainted 60x/sec forever whether or not
// anything changed — ~227% CPU on an idle page. It must stop when settled and
// wake on interaction. An EARLIER attempt at this was reverted for stuck-zoom and
// stuck-motion, so waking is asserted too, not just idling.
//
// Renders are counted via the WebGL draw counter, NOT requestAnimationFrame: the
// page still services rAF while the library is paused, so an rAF counter would
// tick forever and pass vacuously.
const has3D = await page.evaluate(() => !!window.MemoraDebug?.fg?.renderer?.()?.info);
check("force-graph: 3D renderer available (WebGL) for the idle checks", has3D);

if (has3D) {
  const frames = () =>
    page.evaluate(() => window.MemoraDebug.fg.renderer().info.render.frame);
  const delta = async (ms) => {
    const a = await frames();
    await page.waitForTimeout(ms);
    return (await frames()) - a;
  };
  // Poll until renders stop rather than sleeping a fixed time: engine cooldown
  // plus the idle timer is ~7.5s, and a cold worker shifts it by seconds.
  const waitUntilIdle = async (budget = 30000) => {
    const start = Date.now();
    while (Date.now() - start < budget) {
      const a = await frames();
      await page.waitForTimeout(500);
      if ((await frames()) - a === 0) return Date.now() - start;
    }
    return -1;
  };

  check("force-graph: renders advance while the engine is hot", (await delta(1200)) > 5);

  const idledAfter = await waitUntilIdle();
  const idleRenders = idledAfter < 0 ? -1 : await delta(2500);
  check(
    "force-graph: render loop stops when untouched",
    idledAfter >= 0 && idleRenders === 0,
    idledAfter < 0 ? "never idled within 30s" : `idled after ${idledAfter}ms, then ${idleRenders} renders`,
  );

  await page.evaluate(() => document.querySelector("#graph").dispatchEvent(
    new WheelEvent("wheel", { bubbles: true, deltaY: -240 })
  ));
  check(
    "force-graph: interaction wakes the loop after idling",
    (await delta(1200)) > 5,
    "regression guard for the stuck-zoom bug that reverted the first attempt",
  );
}

// Timeline type filter: SUPERSEDED / DUPLICATED (keys graph supersededIds + duplicateIds).
const trowIds = () => page.$$eval(".trow .day", (els) =>
  els.map((e) => {
    const m = e.textContent.match(/#(\d+)/);
    return m ? Number(m[1]) : null;
  }).filter((n) => n != null).sort((a, b) => a - b));
const waitTimeline = async () => {
  await page.waitForFunction(() => document.querySelectorAll(".trow").length > 0, null, { timeout: 20000 });
};
const selectTl = async (kind, typeValue) => {
  await page.evaluate(({ kind, typeValue }) => {
    const sel = document.getElementById("tltype");
    const i = [...sel.options].findIndex((o) =>
      kind === "type"
        ? o.dataset.tlkind === "type" && o.value === typeValue
        : o.dataset.tlkind === kind);
    if (i < 0) throw new Error("tltype option not found: " + kind + " " + (typeValue || ""));
    sel.selectedIndex = i;
    sel.dispatchEvent(new Event("change"));
  }, { kind, typeValue });
  await page.waitForTimeout(250);
};
await waitTimeline();
const defaultIds = await trowIds();
await selectTl("superseded");
const supersededIds = await trowIds();
const supersededStyled = await page.$$eval(".trow", (els) =>
  els.length > 0 && els.every((e) => e.classList.contains("superseded-row")));
check(
  "force-graph: SUPERSEDED filter lists exactly the fixture superseded ids",
  JSON.stringify(supersededIds) === JSON.stringify([1, 2, 6, 12]) && supersededStyled,
  `got ${JSON.stringify(supersededIds)} styled=${supersededStyled}`,
);
await selectTl("duplicated");
const duplicatedIds = await trowIds();
check(
  "force-graph: DUPLICATED filter lists exactly the fixture duplicate ids",
  JSON.stringify(duplicatedIds) === JSON.stringify([8, 9, 12]),
  `got ${JSON.stringify(duplicatedIds)}`,
);
await selectTl("all");
const restoredIds = await trowIds();
check(
  "force-graph: All types restores the default timeline row set",
  JSON.stringify(restoredIds) === JSON.stringify(defaultIds) && defaultIds.length >= 7,
  `default=${JSON.stringify(defaultIds)} restored=${JSON.stringify(restoredIds)}`,
);
await selectTl("type", "__superseded");
const collisionIds = await trowIds();
check(
  "force-graph: real type __superseded filters to that memory only",
  JSON.stringify(collisionIds) === JSON.stringify([10]),
  `got ${JSON.stringify(collisionIds)}`,
);
const xssType = 'a"><img src=x>';
await selectTl("type", xssType);
const xssIds = await trowIds();
const selectClean = await page.$eval("#tltype", (sel) =>
  sel.querySelectorAll("img, b").length === 0
  && [...sel.options].some((o) => o.dataset.tlkind === "type" && o.value === 'a"><img src=x>'));
check(
  "force-graph: markup type option is unescaped-safe and filters exactly",
  JSON.stringify(xssIds) === JSON.stringify([11]) && selectClean,
  `got ${JSON.stringify(xssIds)} clean=${selectClean}`,
);
await selectTl("all");
await page.evaluate(() => {
  const box = document.getElementById("lineage");
  if (box.checked) { box.checked = false; box.dispatchEvent(new Event("change")); }
});
await page.waitForTimeout(400);
const hiddenSuperseded = await trowIds();
check(
  "force-graph: current-only All types excludes superseded ids",
  [1, 2, 6, 12].every((id) => !hiddenSuperseded.includes(id)),
  `got ${JSON.stringify(hiddenSuperseded)}`,
);
await selectTl("superseded");
const revealed = await trowIds();
check(
  "force-graph: current-only SUPERSEDED still lists [1,2,6,12]",
  JSON.stringify(revealed) === JSON.stringify([1, 2, 6, 12]),
  `got ${JSON.stringify(revealed)}`,
);
await selectTl("all");
const hiddenAgain = await trowIds();
check(
  "force-graph: All types after SUPERSEDED hides superseded again",
  [1, 2, 6, 12].every((id) => !hiddenAgain.includes(id)),
  `got ${JSON.stringify(hiddenAgain)}`,
);
await selectTl("duplicated");
const dupsCurrentOnly = await trowIds();
check(
  "force-graph: current-only DUPLICATED hides superseded dups",
  JSON.stringify(dupsCurrentOnly) === JSON.stringify([8, 9]),
  `got ${JSON.stringify(dupsCurrentOnly)}`,
);
await page.evaluate(() => {
  const box = document.getElementById("lineage");
  if (!box.checked) { box.checked = true; box.dispatchEvent(new Event("change")); }
});
await selectTl("all");
await selectTl("superseded");
await page.click(".trow");
await page.waitForTimeout(400);
const panelOpen = await page.evaluate(() =>
  document.body.classList.contains("panel-open") && !!document.querySelector("#panel h2"));
check(
  "force-graph: SUPERSEDED row click still opens detail",
  panelOpen,
  await page.evaluate(() => document.querySelector("#panel h2")?.textContent || "no panel"),
);
await selectTl("all");

// ------------------------------------------- force-graph truncation banner

let fgInjectTruncated = true;
const fgTotal = 5000;
let fgNodeCount = 0;
await page.route("**/api/graph*", async (route) => {
  const url = route.request().url();
  if (!/\/api\/graph(\?|$)/.test(new URL(url).pathname)) {
    await route.continue();
    return;
  }
  const resp = await fetch(url);
  const json = await resp.json();
  fgNodeCount = (json.nodes || []).length;
  json.truncated = fgInjectTruncated;
  json.total = fgTotal;
  await route.fulfill({ json, status: 200, contentType: "application/json" });
});

await page.goto(`${BASE}/force-graph.html`, { waitUntil: "networkidle" });
await page.waitForSelector("#truncation-banner-text", { timeout: 45000 });
await page.waitForTimeout(1200);
const fgBannerVisible = await page.$eval(
  "#truncation-banner",
  (n) => getComputedStyle(n).display === "flex",
);
const fgBannerText = await page.$eval("#truncation-banner-text", (n) => n.textContent);
check(
  "force-graph: truncated banner visible with showing N of 5000 nodes",
  fgBannerVisible && fgBannerText === `showing ${fgNodeCount} of ${fgTotal} nodes`,
  `text="${fgBannerText}" count=${fgNodeCount}`,
);

const [fgRaiseResp] = await Promise.all([
  page.waitForResponse((r) => r.url().includes("/api/graph")),
  page.click("#truncation-raise"),
]);
const fgRaiseUrl = fgRaiseResp.url();
const fgRaiseLimit = Number(new URL(fgRaiseUrl).searchParams.get("limit"));
check(
  "force-graph: raise control sends ?limit=K with 2000 < K <= 5000",
  fgRaiseLimit > 2000 && fgRaiseLimit <= 5000,
  `url=${fgRaiseUrl} limit=${fgRaiseLimit}`,
);

// Second raise reaches the hard max (2000 → 3500 → 5000) while still truncated.
const [fgRaise2] = await Promise.all([
  page.waitForResponse((r) => r.url().includes("/api/graph")),
  page.click("#truncation-raise"),
]);
const fgMaxLimit = Number(new URL(fgRaise2.url()).searchParams.get("limit"));
const fgAtMax = await page.$eval("#truncation-raise", (n) => n.disabled);
const fgMaxLabel = await page.$eval("#truncation-raise", (n) => n.textContent);
const fgBannerStill = await page.$eval(
  "#truncation-banner",
  (n) => getComputedStyle(n).display === "flex",
);
check(
  "force-graph: at hard max + truncated, banner stays and control disabled",
  fgMaxLimit === 5000 && fgBannerStill && fgAtMax && /5000/.test(fgMaxLabel),
  `limit=${fgMaxLimit} banner=${fgBannerStill} disabled=${fgAtMax} label="${fgMaxLabel}"`,
);

// Follow-up with truncated=false -> banner is gone.
fgInjectTruncated = false;
const [fgClearResp] = await Promise.all([
  page.waitForResponse((r) => r.url().includes("/api/graph")),
  page.evaluate(() => { window.doRefresh && window.doRefresh(); }),
]);
await page.waitForTimeout(300);
check(
  "force-graph: follow-up truncated=false hides the banner",
  await page.$eval("#truncation-banner", (n) => getComputedStyle(n).display === "none"),
);
await page.unroute("**/api/graph*");

check("no uncaught page errors", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));

check("browser harness: expected assertion count",
  pass.length + fail.length + 1 === EXPECTED_CHECKS,
  "expected=" + EXPECTED_CHECKS + " actual=" + (pass.length + fail.length + 1));

await browser.close();

console.log("");
pass.forEach((p) => console.log(`  PASS  ${p}`));
fail.forEach((f) => console.log(`  FAIL  ${f}`));
console.log(`\n${pass.length} passed, ${fail.length} failed`);
process.exit(fail.length ? 1 : 0);
