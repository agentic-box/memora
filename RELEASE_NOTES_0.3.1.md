# memora 0.3.1

A patch release. No schema change, no embedding rebuild, no action required on upgrade.

Two things motivated it: the graph UI was heating the machine badly, and 0.3.0 shipped with an
internally inconsistent version number.

## The power problem

**force-graph's 3D view repainted 60 times a second forever, whether or not anything changed.**
Measured on a reporter's Mac with the page completely untouched: **~227% CPU** — over two full cores —
with the browser's GPU helper process alone pinned at 140%. The 2D view cost ~37% under the same
conditions. A static picture of a settled graph was doing continuous work.

**Idle now costs essentially nothing: ~227% → 0.4%.** Once the layout settles the render loop stops,
and the GPU helper process drops out of the process list entirely. It wakes instantly on zoom, drag,
click or scroll.

This was measured end to end in the reporter's own browser (LibreWolf) against a real 771-memory
store, before and after — not inferred from a synthetic benchmark.

### Why this needed care

An earlier attempt at exactly this fix was reverted for two bugs: zoom would stick, and after a while
motion would stop altogether. The idea was never wrong — the mechanism was. `wake()` called the
library's `resumeAnimation()` **unconditionally** and was wired to `pointermove`, so adding a
timer-driven pause meant pause and resume interleaving dozens of times a second, desyncing the
library's own frame bookkeeping.

The fix makes `renderPaused` the single source of truth and calls the library's pause/resume **only on
a real state transition**. High-frequency callers just re-arm a timer. Discrete input
(`pointerdown`/`wheel`/`touchstart`) goes through a separate path that resyncs unconditionally — safe
precisely because those events are rare, and it is the escape hatch if the flag ever drifts. Pausing
additionally requires the physics engine to have settled, so a layout is never frozen mid-settle.

Both historical bugs are covered by regression tests that reproduce them on purpose. Renders are
counted via the WebGL draw counter rather than a `requestAnimationFrame` probe — a raw rAF counter
keeps ticking while the library is paused and would pass vacuously.

**Kill switch:** `localStorage.setItem("memora-graph.noIdle", "1")` disables idling entirely and
restores the previous always-render behaviour, no redeploy needed. Remove the key to re-enable.

## Also in the graph UI

- **Resizable drawers in force-graph.** The timeline and the memory-detail drawers now have
  independent drag handles. Each remembers its own width, clamped between 280px and 90% of the
  viewport, re-clamped on window resize so a width saved on a large display cannot swallow a laptop
  screen. Double-click a handle to reset that drawer.
- **Per-tab panel widths in the default graph view.** The side panel hosts several tabs; a timeline
  list reads fine narrow while memory content wants to be wide, so each tab now keeps its own width.
  The old cap of 800px is gone.
- **The 2D/3D choice persists.** Previously every reload silently returned you to the expensive
  renderer even if you had chosen 2D.
- **Cheaper frames while active:** the WebGL pixel ratio is capped at 1.5 (an uncapped Retina display
  was drawing ~4x the pixels it needed — measured 1.78x less fill per frame), sphere geometry is
  lighter, and the renderer asks the OS for the energy-efficient GPU.

## Fixed

- **`agent.yaml` and `pyproject.toml` disagreed on the version.** 0.3.0 bumped the package but not the
  manifest, so the published tag claimed two different version numbers. A test for exactly this
  already existed and was not run before tagging. Both sources now move together.

## Known issues

Unchanged from 0.3.0 and still open — see the 0.3.0 notes for detail: the epoch time-of-check window;
`memories_embedding_repairs` being unbounded with no foreign key to memory lifetime; D1 ownership
recovery after a lost response lacking bounded retry; and the default `index.html` view not carrying
force-graph's database-switch protection.

## Upgrading

Nothing to do. If you were avoiding the 3D graph because of heat, it is worth another look.
