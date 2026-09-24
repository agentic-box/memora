/**
 * PG1: /api/graph answers for every stored value -- keys named like
 * Object.prototype properties (constructor, toString, __proto__, ...) and odd
 * JSON values (null or non-iterable tags, null metadata, a path it cannot
 * slice, a non-string subsection) used to throw (a 500) or drop a tag colour.
 * Imports the SHIPPED graph.ts (no reimplementation) over an in-memory D1.
 * Run: node --experimental-strip-types memora-graph/scripts/test_graph_keys.mjs
 */

import { onRequestGet } from "../functions/api/graph.ts";

let failures = 0;
function check(name, cond, detail = "") {
  if (cond) {
    console.log(`ok   ${name}`);
  } else {
    failures++;
    console.log(`FAIL ${name} ${detail}`);
  }
}

function fakeD1(memories, crossrefs = []) {
  return {
    prepare(sql) {
      return {
        bind() { return this; },
        async all() {
          if (/FROM memories_crossrefs/.test(sql)) return { results: crossrefs };
          if (/FROM (tombstone_components|tombstones)/.test(sql)) return { results: [] };
          if (/FROM memories/.test(sql)) return { results: memories };
          throw new Error(`unexpected query: ${sql}`);
        },
      };
    },
  };
}

async function graph(memories, query = "", crossrefs = []) {
  const env = { DB_CONFIG: JSON.stringify({ store: "STORE" }), DB_STORE: fakeD1(memories, crossrefs) };
  try {
    const res = await onRequestGet({ env, request: new Request(`https://pages.local/api/graph?db=store${query}`) });
    return { status: res.status, body: await res.json() };
  } catch (err) {  // what Pages turns into a 500
    return { status: "threw", body: { error: String(err) } };
  }
}

let nextId = 1;
function mem(content, metadata, tags, created = "2026-09-01") {
  return { id: nextId++, content, metadata, tags, created_at: created, updated_at: null };
}

// ---------------------------------------------------------------- prototype-named keys

nextId = 1;
const proto = [
  mem("plain", "{}", '["alpha"]', "2026-09-09"),
  mem("tag constructor", "{}", '["constructor"]', "2026-09-08"),
  mem("second tag toString", "{}", '["beta", "toString"]', "2026-09-07"),
  mem("section hasOwnProperty", '{"section": "hasOwnProperty"}', '["alpha"]', "2026-09-06"),
  mem("issue component valueOf", '{"type": "issue", "component": "valueOf"}', '["alpha"]', "2026-09-05"),
  mem("todo category __proto__", '{"type": "todo", "category": "__proto__"}', '["__proto__"]', "2026-09-04"),
  mem("issue status isPrototypeOf", '{"type": "issue", "status": "isPrototypeOf"}', '["alpha"]', "2026-09-03"),
  mem("todo status toString", '{"type": "todo", "status": "toString"}', '["alpha"]', "2026-09-02"),
];
{
  const { status, body } = await graph(proto);
  check("prototype-named keys answer 200", status === 200, JSON.stringify(body).slice(0, 200));
  if (status === 200) {
  check("a 'constructor' tag gets its own colour", typeof body.tagColors?.constructor === "string"
    && body.tagColors.constructor.startsWith("#"));
  check("a '__proto__' tag gets its own colour and mapping",
    Object.hasOwn(body.tagColors, "__proto__") && JSON.stringify(body.tagToNodes.__proto__) === "[6]");
  check("tagToNodes maps 'constructor' and 'toString'",
    JSON.stringify(body.tagToNodes.constructor) === "[2]" && JSON.stringify(body.tagToNodes.toString) === "[3]");
  check("sectionToNodes maps 'hasOwnProperty'", JSON.stringify(body.sectionToNodes.hasOwnProperty) === "[4]");
  check("issueCategoryToNodes maps 'valueOf'", JSON.stringify(body.issueCategoryToNodes.valueOf) === "[5]");
  check("todoCategoryToNodes maps '__proto__'", JSON.stringify(body.todoCategoryToNodes.__proto__) === "[6]");
  check("statusToNodes maps 'isPrototypeOf'", JSON.stringify(body.statusToNodes.isPrototypeOf) === "[7]");
  const n7 = body.nodes.find(n => n.id === 7);
  check("an unknown issue status takes the open colour", n7?.color === "#ff7b72", JSON.stringify(n7));
  const n8 = body.nodes.find(n => n.id === 8);
  check("an unknown TODO status takes the open colour", n8?.color === "#58a6ff"
    && JSON.stringify(body.todoStatusToNodes.toString) === "[8]", JSON.stringify(n8));
  check("every node has a colour", body.nodes.every(n => n.color !== undefined));
  }
}

// A fragment's tag: never mapped, but its colour used to be skipped.
nextId = 1;
{
  const rows = [
    mem("plain", "{}", '["alpha"]', "2026-09-09"),
    mem("root", '{"type": "document_root", "document_key": "k"}', '["alpha"]', "2026-09-08"),
    mem("fragment", '{"type": "document_fragment", "document_key": "k", "ordinal": 1}', '["constructor"]', "2026-09-01"),
  ];
  const { status, body } = await graph(rows, "&docs=1");
  check("a fragment's 'constructor' tag gets a colour (docs=1)", status === 200
    && body.tagColors?.constructor === "#c084fc", JSON.stringify(body.tagColors));
}

// ---------------------------------------------------------------- values that used to throw

nextId = 1;
const odd = [
  mem("plain", "{}", '["b"]', "2026-09-30"),
  mem("path string of two units", '{"hierarchy": {"path": "XY"}}', '["a"]', "2026-09-27"),
  mem("object path with a length", '{"hierarchy": {"path": {"length": 1, "0": "A"}}}', '["a"]', "2026-09-26"),
  mem("subsection a number", '{"section": "S", "subsection": 5}', '["a"]', "2026-09-25"),
  mem("metadata null", "null", '["a"]', "2026-09-24"),
  mem("tags null", "{}", "null", "2026-09-23"),
  mem("tags an object", "{}", '{"0": "z"}', "2026-09-22"),
  mem("tags a number", "{}", "5", "2026-09-21"),
  mem("fragment tags null", '{"type": "document_fragment", "document_key": "k"}', "null", "2026-09-20"),
  mem("path of one unit", '{"hierarchy": {"path": "X"}}', '["a"]', "2026-09-19"),
];
for (const query of ["", "&docs=1"]) {
  const { status, body } = await graph(odd, query);
  check(`odd values answer 200 (${query || "no query"})`, status === 200, JSON.stringify(body).slice(0, 200));
  if (status !== 200) continue;
  const sections = body.sectionToNodes;
  check(`an unsliceable path is treated as absent (${query || "-"})`,
    JSON.stringify(sections.Uncategorized) === "[1,2,3,5,6,7,8]", JSON.stringify(sections));
  check(`a numeric subsection is ignored (${query || "-"})`, JSON.stringify(sections.S) === "[4]"
    && !Object.keys(body.subsectionToNodes).some(k => k.startsWith("S/")));
  check(`a one-unit path string is the section (${query || "-"})`, JSON.stringify(sections.X) === "[10]");
  check(`null / non-iterable tags map nothing and take 'untagged' (${query || "-"})`,
    body.nodes.find(n => n.id === 6)?.color === body.tagColors.untagged
    && !Object.values(body.tagToNodes).some(ids => ids.includes(6) || ids.includes(8)));
  check(`an object's tags[0] is still its primary tag (${query || "-"})`, typeof body.tagColors.z === "string");
}

// ---------------------------------------------------------------- PG2: values that carry their own toString

nextId = 1;
{
  const own = '{"toString": 0}';
  const rows = [
    mem("plain", "{}", '["b"]', "2026-09-30"),
    mem("object tag", "{}", `[${own}]`, "2026-09-29"),
    mem("nested tag", "{}", `["x", [${own}, null]]`, "2026-09-28"),
    mem("object section", `{"section": ${own}}`, '["a"]', "2026-09-27"),
    mem("object path part", `{"hierarchy": {"path": ["T", ${own}]}}`, '["a"]', "2026-09-26"),
    mem("object issue status", `{"type": "issue", "status": ${own}, "component": ${own}}`, '["a"]', "2026-09-25"),
    mem("object closed_reason", `{"type": "todo", "status": "closed", "closed_reason": ${own}, "category": ${own}}`,
      '["a"]', "2026-09-24"),
  ];
  const { status, body } = await graph(rows);
  check("values with their own toString answer 200", status === 200, JSON.stringify(body).slice(0, 200));
  if (status === 200) {
    check("an object tag is '[object Object]'", JSON.stringify(body.tagToNodes["[object Object]"]) === "[2]"
      && typeof body.tagColors["[object Object]"] === "string");
    check("a nested tag joins as String() does", JSON.stringify(body.tagToNodes["[object Object],"]) === "[3]");
    check("an object section and path part", JSON.stringify(body.sectionToNodes["[object Object]"]) === "[4]"
      && JSON.stringify(body.subsectionToNodes["T/[object Object]"]) === "[5]");
    check("an object status and component", JSON.stringify(body.statusToNodes["[object Object]"]) === "[6]"
      && JSON.stringify(body.issueCategoryToNodes["[object Object]"]) === "[6]"
      && body.nodes.find(n => n.id === 6)?.color === "#ff7b72");
    check("an object closed_reason and category", JSON.stringify(body.todoStatusToNodes["closed:[object Object]"]) === "[7]"
      && JSON.stringify(body.todoCategoryToNodes["[object Object]"]) === "[7]");
  }
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
