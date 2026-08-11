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

// ---------------------------------------------------------------- force-graph

await page.goto(`${BASE}/force-graph.html`, { waitUntil: "networkidle" });
await page.waitForFunction(() => !!window.MemoraDebug?.fg, null, { timeout: 45000 });
await page.waitForTimeout(2500);

// Open the detail drawer so BOTH drawers are open (timeline is open by default).
await page.click(".trow", { timeout: 15000 }).catch(() => {});
await page.waitForTimeout(1200);

const geo = await page.evaluate(() => {
  const rect = (sel) => {
    const n = document.querySelector(sel);
    if (!n) return null;
    const b = n.getBoundingClientRect();
    return { left: Math.round(b.left), right: Math.round(b.right) };
  };
  return {
    classes: document.body.className,
    bar: rect("#bar"),
    timeline: rect("#timeline"),
    panel: rect("#panel"),
  };
});
const bothOpen = /timeline-open/.test(geo.classes) && /panel-open/.test(geo.classes);
check("force-graph: both drawers open for the geometry checks", bothOpen, geo.classes.trim());

// SHIPPED BUG: the drawers are fixed, full-height and stack ABOVE #bar (z-index
// 31/32 vs 30), but the bar was sized to the whole viewport — with both drawers
// open its right-hand controls were painted over and unreachable.
if (bothOpen && geo.bar) {
  const drawerLeft = Math.min(
    geo.timeline?.left ?? Number.MAX_SAFE_INTEGER,
    geo.panel?.left ?? Number.MAX_SAFE_INTEGER,
  );
  check(
    "force-graph: top bar stays clear of the open drawers",
    geo.bar.right <= drawerLeft,
    `bar.right=${geo.bar.right} drawer.left=${drawerLeft}`,
  );
}

// The drawers are independently resizable and each remembers its own width.
await page.evaluate(() => {
  localStorage.setItem("memora-graph.width.timeline", "420");
  localStorage.setItem("memora-graph.width.detail", "640");
});
await page.reload({ waitUntil: "networkidle" });
await page.waitForFunction(() => !!window.MemoraDebug?.fg, null, { timeout: 45000 });
await page.waitForTimeout(1500);
const widths = await page.evaluate(() => ({
  timeline: localStorage.getItem("memora-graph.width.timeline"),
  detail: localStorage.getItem("memora-graph.width.detail"),
  cssTimeline: getComputedStyle(document.documentElement)
    .getPropertyValue("--timeline-width").trim(),
}));
check(
  "force-graph: drawer widths persist independently",
  widths.timeline === "420" && widths.detail === "640" && widths.cssTimeline === "420px",
  `timeline=${widths.timeline} detail=${widths.detail} css=${widths.cssTimeline}`,
);

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

  await page.mouse.move(500, 400);
  await page.mouse.wheel(0, -240);
  check(
    "force-graph: interaction wakes the loop after idling",
    (await delta(1200)) > 5,
    "regression guard for the stuck-zoom bug that reverted the first attempt",
  );
}

if (pageErrors.length) {
  check("no uncaught page errors", false, pageErrors.slice(0, 2).join(" | "));
}

await browser.close();

console.log("");
pass.forEach((p) => console.log(`  PASS  ${p}`));
fail.forEach((f) => console.log(`  FAIL  ${f}`));
console.log(`\n${pass.length} passed, ${fail.length} failed`);
process.exit(fail.length ? 1 : 0);
