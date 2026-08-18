/**
 * Pure-logic tests for the truncation-banner / limit-raise helpers.
 * Imports the SHIPPED module (public/_graph_limit.mjs) — no mirrors.
 * Run: node --experimental-strip-types scripts/test_graph_limit_ui.mjs
 */

import {
  buildGraphUrl,
  formatTruncationBanner,
  nextGraphLimit,
  GRAPH_DEFAULT_LIMIT,
  GRAPH_LIMIT_MAX,
} from "../public/_graph_limit.mjs";

let failed = 0;
function assert(cond, message) {
  if (!cond) {
    console.error("FAIL:", message);
    failed++;
  } else {
    console.log("ok:", message);
  }
}

// Banner hidden when truncated === false.
{
  const r = formatTruncationBanner({ truncated: false, shown: 3, total: 5 });
  assert(r.visible === false, "banner hidden when truncated=false");
}

// Banner hidden when truncated is missing/undefined.
{
  const r = formatTruncationBanner({ shown: 3, total: 5 });
  assert(r.visible === false, "banner hidden when truncated missing/undefined");
}

// Banner visible with exact "showing 3 of 5 nodes".
{
  const r = formatTruncationBanner({ truncated: true, shown: 3, total: 5 });
  assert(r.visible === true, "banner visible when truncated=true");
  assert(r.text === "showing 3 of 5 nodes", `banner text exact: got "${r.text}"`);
}

// Banner uses the provided shown/total, not a hardcoded 2000.
{
  const r = formatTruncationBanner({ truncated: true, shown: 7, total: 42 });
  assert(r.text === "showing 7 of 42 nodes", `banner uses provided shown/total: got "${r.text}"`);
}

// nextGraphLimit: current=2000, hardMax=5000 -> in (2000, 5000].
{
  const n = nextGraphLimit({ current: 2000, hardMax: 5000 });
  assert(n > 2000 && n <= 5000, `next(2000,5000) in (2000,5000]: got ${n}`);
}

// nextGraphLimit: current=5000, hardMax=5000 -> 5000.
{
  const n = nextGraphLimit({ current: 5000, hardMax: 5000 });
  assert(n === 5000, `next(5000,5000) === 5000: got ${n}`);
}

// nextGraphLimit never exceeds hardMax (current=4999 and current=6000).
{
  const a = nextGraphLimit({ current: 4999, hardMax: 5000 });
  const b = nextGraphLimit({ current: 6000, hardMax: 5000 });
  assert(a > 4999 && a <= 5000, `next(4999,5000) is >4999 and <=5000: got ${a}`);
  assert(b === 5000 && b <= 5000, `next(6000,5000) === 5000 (never exceeds): got ${b}`);
}

// buildGraphUrl default omits limit.
{
  const u = buildGraphUrl({ pathname: "/api/graph", db: "memora", docs: false, limit: null });
  assert(!/limit=/.test(u), `default omits limit: got "${u}"`);
  assert(u === "/api/graph?db=memora", `default preserves db: got "${u}"`);
}

// buildGraphUrl raised includes ?limit=N.
{
  const u = buildGraphUrl({ pathname: "/api/graph", db: "memora", docs: false, limit: 3500 });
  assert(/limit=3500/.test(u), `raised includes ?limit=3500: got "${u}"`);
}

// buildGraphUrl clamps limit > 5000 down to 5000.
{
  const u = buildGraphUrl({ pathname: "/api/graph", db: "memora", docs: false, limit: 9000 });
  assert(/limit=5000/.test(u) && !/limit=9000/.test(u), `clamps limit>5000 to 5000: got "${u}"`);
}

// Constants are exported and sane.
{
  assert(GRAPH_DEFAULT_LIMIT === 2000, "GRAPH_DEFAULT_LIMIT === 2000");
  assert(GRAPH_LIMIT_MAX === 5000, "GRAPH_LIMIT_MAX === 5000");
}

if (failed) {
  console.error(`\n${failed} graph-limit-ui test(s) failed`);
  process.exit(1);
}
console.log("\nAll graph-limit-ui tests passed");
