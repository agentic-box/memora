# memora 0.3.2

**If you installed 0.3.0 or 0.3.1 from scratch, your server was almost certainly dead. Upgrade.**

## Fresh installs were broken

`mcp` 2.0.0 (2026-07-28) removed `mcp.server.fastmcp` — the module was renamed to
`mcp.server.mcpserver` and `FastMCP` to `MCPServer`. `memora/server.py` imports the old path, and
the dependency was declared as an unbounded `mcp>=1.0.0`, so **every fresh install resolved to the
new major and the server died at import**:

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

Fixed by constraining to `mcp>=1.0.0,<2`. `mcp` 1.29.0 shipped the same day as 2.0.0, so the 1.x line
is maintained in parallel — this is a deliberate bound, not a dead end.

**Reported and fixed by [@BillyBunn](https://github.com/BillyBunn) in
[#44](https://github.com/agentic-box/memora/pull/44).**

### Why nobody noticed

Two reasons, both worth stating plainly.

**The failure is silent from the client side.** A dead stdio MCP server is indistinguishable from one
that exposes no tools: the client launches it, the process exits, the client reports zero tools, and
nothing surfaces an error. The reporter had it serving zero tools to three separate MCP clients on a
fork before noticing by accident.

**And our own environments were immune.** Existing installs had `mcp` pinned to a 1.x resolved before
2.0.0 existed, so the test suite passed everywhere it was run. A green suite in a warm environment
cannot detect a dependency-resolution bug; only installing into an empty environment can. 0.3.2 was
verified that way — a fresh venv resolves `mcp 1.29.0` and `memora.server` imports cleanly.

A port to `mcp` 2.x is deliberately **not** in this release. It is more than a rename:
`_sanitize_tool_schemas` reaches into FastMCP internals (`server._tool_manager._tools`), the `@tool()`
decorator's return value changed, and `host`/`port` moved off the constructor onto `run()`. That work
is worth doing separately from unbreaking installs.

## Behaviour change: issues are no longer inferred

**`memory_create_issue` and `memory_create_todo` are now the only ways a memory becomes an issue or a
TODO.** Content written through `memory_absorb` or a plain `memory_create` stays untyped knowledge.

A keyword classifier used to stamp `type=issue` / `status=open` onto anything whose text contained
enough bug vocabulary. It mislabelled **130 knowledge memories** in a real store — standing rules,
session notes, review outputs, research. Two mechanical defects were fixed first:

- **substring bleed** — `"fault"` matched inside DE*FAULT*, `"patch"` inside DIS*PATCH*, `"bug"`
  inside DE*BUG*, `"fix"` inside *FIX*TURES, `"issue"` inside *ISSUE*D
- **double counting** — `"resolve"` and `"resolved"` were separate entries, so a single occurrence of
  "resolved" scored 2 and cleared the threshold by itself

That removed the spurious hits but not the real limitation: word frequency cannot distinguish a note
*about* a bug from a bug *report*. The write-up of that very fix scored five legitimate whole-word
hits and was filed as an open issue. So the feature was removed rather than tuned further.

If you relied on auto-classification, existing typed memories are untouched — only new writes change.

## memora-graph

- **The database selector now lists every configured database.** The default graph page hardcoded the
  names in two places — the `<option>` markup and a `?db=` whitelist — so a third database was both
  invisible in the dropdown and unreachable by URL, while the force-graph view (which has always read
  `/api/databases`) listed it correctly. The list now comes from the Worker's bindings in both views.
- **The top bar no longer disappears behind open drawers.** The timeline and detail drawers are
  fixed, full-height and stack above the bar, but the bar was sized to the whole viewport — with both
  open, six of its controls were painted over and unreachable.

## Upgrading

Nothing to do beyond upgrading. No schema change, no embedding rebuild.
