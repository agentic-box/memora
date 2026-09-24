"""G4: memora-all's /api/graph payload is exactly the Pages viewer's
(functions/api/graph.ts onRequestGet + _lineage.ts) for the same rows:
every node field, every edge (edge_type/score/directed), lineage,
retirement, duplicates, flags, mappings, timeline, clusters, ?docs=1 and
?limit=.

tests/fixtures/g4_pages_payloads.json holds the payload the REAL Pages code
returned for each scenario store below and each query (an esbuild bundle of
graph.ts run by Node over the SQLite file through a read-only D1 shim).
Regenerate: ``python -m tests.test_g4_graph_payload OUTDIR`` writes the
stores; run the bundle on each with each query of QUERIES and record
{scenario: {query: payload}}.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

from memora.graph import data, payload

FIXTURE = Path(__file__).parent / "fixtures" / "g4_pages_payloads.json"
QUERIES = ["", "&docs=1", "&limit=12", "&docs=1&limit=25"]
SCENARIOS = ["lineage", "content", "docs", "corrupt_row", "no_crossrefs_table", "retirement_fails", "random",
             "unicode_times", "odd_values"]
# Inputs on which graph.ts itself fails (leader 8135): no parity owed, a correct payload here.
DEFECT_SCENARIOS = ["prototype_keys", "prototype_fragment", "malformed_throws"]


def _meta(**kw):
    return json.dumps(kw)


def _rows_lineage():
    mem, refs = [], {}
    for i in range(1, 41):
        mem.append((i, f"memory {i} about topic {i % 5}", _meta(), json.dumps([f"t{i % 3}"]),
                    f"2026-09-{1 + i % 9:02d} 12:00:00"))
    refs[1] = [{"id": 2, "score": 0.9, "edge_type": "supersedes"}, {"id": 3, "edge_type": "references", "score": 0.7}]
    refs[2] = [{"id": 1, "score": 0.8, "edge_type": "superseded_by"}]          # the other half, another score
    refs[4] = [{"id": 5, "edge_type": "supersedes"}]                           # no score -> 1
    refs[5] = [{"id": 4, "edge_type": "supersedes", "score": 0.5}]             # a 2-cycle
    refs[6] = [{"id": 7, "edge_type": "supersedes", "score": 0.6}]
    refs[7] = [{"id": 8, "edge_type": "supersedes", "score": 0.6}]
    refs[8] = [{"id": 6, "edge_type": "supersedes", "score": 0.6}]             # a 3-cycle
    refs[9] = [{"id": 9, "edge_type": "supersedes", "score": 0.9}, {"id": 9, "score": 0.99}]  # self
    refs[10] = [{"id": 12, "edge_type": "supersedes", "score": 0.7}]
    refs[11] = [{"id": 12, "edge_type": "supersedes", "score": 0.7}]           # a fork: 10 and 11 unknown
    refs[13] = [{"id": 99, "edge_type": "supersedes", "score": 0.7}]           # dangling: 99 is no memory
    refs[14] = [{"id": 15, "edge_type": "referenced_by", "score": 0.3}, {"id": 16, "edge_type": "implements"},
                {"id": 17, "edge_type": "implemented_by", "score": 0.95}, {"id": 18, "edge_type": "extends", "score": 0.2},
                {"id": 19, "edge_type": "extended_by", "score": 0.61}, {"id": 20, "edge_type": "contradicts", "score": 0.1},
                {"id": 21, "edge_type": "weird_type", "score": 0.5}, {"id": 22, "score": 0.4},
                {"id": 23, "score": 0.41}, {"id": 24, "edge_type": "related_to", "score": 0.86}]
    refs[15] = [{"id": 14, "edge_type": "references", "score": 0.9}]           # max-merge with 14's half
    refs[23] = [{"id": 14, "score": 0.52}]
    refs[24] = [{"id": 14, "score": 0.9999}, {"id": 25, "score": 0.87}, {"id": 26, "score": 0.88, "edge_type": "extends"}]
    refs[25] = [{"id": 24, "score": 0.85}, {"id": 27, "score": 0.851}]
    refs[28] = [{"id": 29, "score": 0.9999}]                                   # an absorb link, not a duplicate
    refs[30] = [{"id": 31, "score": 0.9}, {"id": 32, "score": 0.9}, {"id": 33, "score": 0.9}]
    refs[31] = [{"id": 30, "score": 0.9}, {"id": 32, "score": 0.9}, {"id": 33, "score": 0.9}]
    refs[34] = [{"id": 35, "edge_type": "supersedes", "score": 0.7}, {"id": 35, "edge_type": "supersedes", "score": 0.8},
                {"id": 35, "edge_type": "supersedes", "score": 0.75}]          # repeated with other scores
    for i in range(36, 41):
        refs[i] = [{"id": j, "score": 0.6} for j in range(36, 41) if j != i]
    tombstones = [(3,), (38,)]
    components = [(38,), (39,)]
    return mem, refs, tombstones, components


def _rows_content():
    rows = []
    add = rows.append
    long_emoji = "x" * 34 + "\U0001F600" + "tail"                    # a pair split at code unit 35
    heading_emoji = "## " + "y" * 58 + "\U0001F4A9" + " more"         # a pair split at code unit 60
    add((1, long_emoji, _meta(), '["alpha"]'))
    add((2, heading_emoji, _meta(), '["alpha", "beta"]'))
    add((3, '\ufeff  "quoted" back\\slash #hash *star* _u_ `t` [b]\u00a0\n2nd line', _meta(), '["beta"]'))
    add((4, "\x1c spaces python strips \x85", _meta(), '[]'))
    add((5, "####", _meta(), '[5, "five"]'))
    add((46, "#\x85heading after a character only Python calls whitespace", _meta(), '["alpha"]'))
    add((6, "#   Title   ", _meta(type="issue"), '["issues"]'))
    add((7, "issue resolved", _meta(type="issue", status="resolved", severity="critical", component="core"), '["issues"]'))
    add((8, "issue wontfix", _meta(type="issue", status="wontfix"), '["issues"]'))
    add((9, "issue in progress", _meta(type="issue", status="in_progress", component=""), '["issues"]'))
    add((10, "issue closed", _meta(type="issue", status="closed", closed_reason="duplicate"), '["issues"]'))
    add((11, "issue closed no reason", _meta(type="issue", status="closed"), '["issues"]'))
    add((12, "issue odd status", _meta(type="issue", status="triaged"), '["issues"]'))
    add((13, "todo open", _meta(type="todo", priority="high", category="ops"), '["todo"]'))
    add((14, "todo completed", _meta(type="todo", status="completed"), '["todo"]'))
    add((15, "todo blocked", _meta(type="todo", status="blocked"), '["todo"]'))
    add((16, "todo closed", _meta(type="todo", status="closed", closed_reason="not_planned"), '["todo"]'))
    add((17, "a section", _meta(type="section"), '["s"]'))
    add((18, "hierarchy", _meta(hierarchy={"path": ["Top", "Mid", "Leaf"]}), '["h"]'))
    add((19, "hierarchy with null", _meta(hierarchy={"path": ["Top", None, 3]}), '["h"]'))
    add((20, "section strings", _meta(section="Sec", subsection="a/b/c"), '["h"]'))
    add((21, "section only", _meta(section="Sec"), '"just a string"'))
    add((22, "empty path", _meta(hierarchy={"path": []}, section="Fallback"), '["h"]'))
    add((23, "bad metadata json", "{not json", '["x"]'))
    add((24, "array metadata", "[1, 2]", '["x"]'))
    add((25, "null created_at", _meta(), '["alpha"]'))
    add((26, "same time as 27", _meta(), '["gamma"]'))
    add((27, "same time as 26", _meta(), '["gamma"]'))
    add((28, "tag with number first", _meta(), '[0, "zero"]'))
    add((29, "tag bool", _meta(), '[true]'))
    add((30, "tag float", _meta(), '[1.5, 1e21, 1e-7]'))
    for i in range(31, 46):
        add((i, f"tag spread {i}", _meta(), json.dumps([f"tag{i}"])))
    mem = []
    for r in rows:
        created = None if r[0] == 25 else ("2026-09-10 10:00:00" if r[0] in (26, 27) else f"2026-08-{1 + r[0] % 28:02d} 09:{r[0] % 60:02d}:00")
        mem.append((r[0], r[1], r[2], r[3], created))
    refs = {1: [{"id": 2, "score": 0.5}, {"id": 3, "score": 0.9}], 2: [{"id": 1, "score": 0.5}],
            6: [{"id": 7, "score": 0.95}], 13: [{"id": 14, "score": 0.44}]}
    # nodes with exactly 2 and 13 connections: V8's log1p differs from macOS libm there
    refs[40] = [{"id": j, "score": 0.7} for j in range(31, 45) if j != 40]
    refs[26] = [{"id": 27, "score": 0.7}, {"id": 28, "score": 0.7}]
    return mem, refs, [], []


def _rows_docs():
    mem, refs = [], {}
    mem.append((1, "doc A root", _meta(type="document_root", document_key="A"), '["doc"]', "2026-09-01 00:00:00"))
    for k, ordinal in enumerate([3, 1, 2, 2, None], start=2):
        m = {"type": "document_fragment", "document_key": "A", "section_heading": f"A part {k}"}
        if ordinal is not None:
            m["ordinal"] = ordinal
        mem.append((k, f"A fragment {k}", json.dumps(m), '["doc"]', f"2026-09-0{k} 00:00:00"))
    for k in range(7, 11):  # doc B: no root -> a chain by ordinal
        mem.append((k, f"B fragment {k}", _meta(type="document_fragment", document_key="B", ordinal=10 - k),
                    '["doc"]', "2026-09-03 00:00:00"))
    mem.append((11, "fragment without key", _meta(type="document_fragment", section_heading=""), '["doc"]', "2026-09-04 00:00:00"))
    mem.append((12, "a section", _meta(type="section"), '["doc"]', "2026-09-05 00:00:00"))
    mem.append((13, "second root for A", _meta(type="document_root", document_key="A"), '["doc"]', "2025-01-01 00:00:00"))
    for k in range(14, 30):
        mem.append((k, f"plain {k}", _meta(), '["p"]', f"2026-07-{k:02d} 00:00:00"))
        refs[k] = [{"id": k + 1, "score": 0.6}, {"id": 2, "score": 0.7}]
    refs[3] = [{"id": 4, "edge_type": "supersedes", "score": 0.9}]
    return mem, refs, [], []


def _rows_random():
    rnd = random.Random(8094)
    mem, refs = [], {}
    types = [None, None, None, "issue", "todo", "section", "document_root", "document_fragment"]
    edge_types = [None, None, "related_to", "supersedes", "superseded_by", "references", "referenced_by",
                  "implements", "extends", "contradicts"]
    for i in range(1, 121):
        t = rnd.choice(types)
        m = {}
        if t:
            m["type"] = t
        if t in ("document_root", "document_fragment"):
            m["document_key"] = rnd.choice(["d1", "d2", "d3"])
            m["ordinal"] = rnd.randrange(5)
        if t in ("issue", "todo"):
            m["status"] = rnd.choice(["open", "closed", "resolved", "completed", "blocked", "in_progress"])
        tags = rnd.sample(["a", "b", "c", "d", "e", "f", "g", "h", "i"], rnd.randrange(0, 3))
        mem.append((i, f"random {i} " + "w" * rnd.randrange(0, 50), json.dumps(m), json.dumps(tags),
                    f"2026-0{rnd.randrange(1, 10)}-{rnd.randrange(10, 29)} 0{rnd.randrange(10)}:00:00"))
        out = []
        for _ in range(rnd.randrange(0, 7)):
            e = {"id": rnd.randrange(1, 130), "score": round(rnd.uniform(0.2, 1.0), 4)}
            et = rnd.choice(edge_types)
            if et:
                e["edge_type"] = et
            if rnd.random() < 0.1:
                del e["score"]
            out.append(e)
        refs[i] = out
    tomb = [(rnd.randrange(1, 121),) for _ in range(5)]
    return mem, refs, tomb, [(tomb[0][0],)]


def _rows_unicode_times():
    """created_at compared as JS compares strings: by UTF-16 code unit
    (review 8133 P2). By code point U+1F600 > U+FFFF; by code unit it is
    D83D < FFFF. U+0100 vs U+0001 also flips under UTF-16-LE bytes."""
    stamps = ["2026-09-30 \U0001F600", "2026-09-30 \uffff", "2026-09-30 \u0100", "2026-09-30 \u0001",
              "2026-09-30 \u00e9", "2026-09-30 z", "2026-09-30 \U00010000", "2026-09-30 \ud7ff"]
    mem = [(i + 1, f"stamp {i + 1}", _meta(), '["u"]', st) for i, st in enumerate(stamps)]
    refs = {1: [{"id": 2, "score": 0.9}, {"id": 3, "score": 0.9}], 2: [{"id": 3, "score": 0.9}]}
    return mem, refs, [], []


# (metadata, tags, is_fragment): odd JSON values graph.ts handles (probed with the bundle)
_ODD_OK = [
    ('{"hierarchy": {"path": "X"}}', '["a"]'),                                   # review 8153: section "X"
    ('{"hierarchy": {"path": ""}, "section": "S"}', '["a"]'),
    ('{"hierarchy": {"path": {"length": 0}}, "section": "S2"}', '["a"]'),
    ('{"hierarchy": {"path": 5}, "section": "S3"}', '["a"]'),
    ('{"hierarchy": ["x"], "section": "S4"}', '["a"]'),
    ('{"hierarchy": {"path": ["T", ["a", null], {"k": 1}]}}', '["a"]'),
    ('"a json string"', '["a"]'),
    ('7', '["a"]'),
    ('{}', '"hello"'),
    ('{}', '[{"k": 1}, ["p", "q"], null]'),
    ('{"type": "issue", "status": {"a": 1}}', '["a"]'),
    ('{"type": "todo", "status": "closed", "closed_reason": ["x", "y"]}', '["a"]'),
    ('{"section": 5}', '["a"]'),
    ('{"section": "S", "subsection": ""}', '["a"]'),
    ('{"type": "document_fragment", "document_key": "k"}', '{"0": "z"}'),       # fragments are never mapped
    ('{"type": "document_fragment", "document_key": "k", "section_heading": 5}', '5'),
]
# values graph.ts throws on: one per memory, a Pages defect each
_ODD_THROWS = [
    ('{"hierarchy": {"path": "XY"}}', '["a"]'),
    ('{"hierarchy": {"path": {"length": 1, "0": "A"}}}', '["a"]'),
    ('{"section": "S", "subsection": 5}', '["a"]'),
    ('null', '["a"]'),
    ('{}', 'null'),
    ('{}', '{"0": "z"}'),
    ('{}', '5'),
    ('{"type": "document_fragment", "document_key": "k"}', 'null'),
]


def _rows_odd(cases):
    mem = [(1, "plain", _meta(), '["b"]', "2026-09-30 00:00:00")]
    for i, (meta, tags) in enumerate(cases, start=2):
        mem.append((i, f"odd {i}", meta, tags, f"2026-09-{29 - i:02d} 00:00:00"))
    return mem, {1: [{"id": 2, "score": 0.9}]}, [], []


def _rows_prototype_keys():
    """Object.prototype names as keys: graph.ts throws (a Pages defect)."""
    mem = [
        (1, "plain", _meta(), '["alpha"]', "2026-09-09"),
        (2, "tag constructor", _meta(), '["constructor"]', "2026-09-08"),
        (3, "second tag toString", _meta(), '["beta", "toString"]', "2026-09-07"),
        (4, "section hasOwnProperty", _meta(section="hasOwnProperty"), '["alpha"]', "2026-09-06"),
        (5, "issue component valueOf", _meta(type="issue", component="valueOf"), '["alpha"]', "2026-09-05"),
        (6, "todo category __proto__", _meta(type="todo", category="__proto__"), '["alpha"]', "2026-09-04"),
        (7, "issue status isPrototypeOf", _meta(type="issue", status="isPrototypeOf"), '["alpha"]', "2026-09-03"),
    ]
    return mem, {1: [{"id": 2, "score": 0.9}]}, [], []


def _rows_prototype_fragment():
    """Only a document fragment carries the name: graph.ts does not throw
    (fragments are never mapped) but leaves the tag out of tagColors."""
    mem = [
        (1, "plain", _meta(), '["alpha"]', "2026-09-09"),
        (2, "root", _meta(type="document_root", document_key="k"), '["alpha"]', "2026-09-08"),
        (3, "fragment", _meta(type="document_fragment", document_key="k", ordinal=1), '["constructor"]', "2026-09-01"),
    ]
    return mem, {}, [], []


def build_store(path, scenario):
    if scenario in ("lineage", "corrupt_row", "no_crossrefs_table", "retirement_fails"):
        mem, refs, tomb, comp = _rows_lineage()
    elif scenario == "content":
        mem, refs, tomb, comp = _rows_content()
    elif scenario == "docs":
        mem, refs, tomb, comp = _rows_docs()
    elif scenario == "random":
        mem, refs, tomb, comp = _rows_random()
    elif scenario == "unicode_times":
        mem, refs, tomb, comp = _rows_unicode_times()
    elif scenario == "odd_values":
        mem, refs, tomb, comp = _rows_odd(_ODD_OK)
    elif scenario == "malformed_throws":
        mem, refs, tomb, comp = _rows_odd(_ODD_THROWS)
    elif scenario == "prototype_keys":
        mem, refs, tomb, comp = _rows_prototype_keys()
    elif scenario == "prototype_fragment":
        mem, refs, tomb, comp = _rows_prototype_fragment()
    else:
        raise ValueError(scenario)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, metadata TEXT, tags TEXT,"
                 " created_at TEXT, updated_at TEXT)")
    conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,NULL)", mem)
    if scenario != "no_crossrefs_table":
        conn.execute("CREATE TABLE memories_crossrefs (memory_id INTEGER PRIMARY KEY, related TEXT)")
        conn.executemany("INSERT INTO memories_crossrefs VALUES (?,?)",
                         sorted(((k, json.dumps(v)) for k, v in refs.items()), reverse=True))
        if scenario == "corrupt_row":
            conn.execute("UPDATE memories_crossrefs SET related = '[{\"id\": 1, \"score\": NaN}]' WHERE memory_id = 6")
    conn.execute("CREATE TABLE tombstones (memory_id INTEGER)")
    conn.executemany("INSERT INTO tombstones VALUES (?)", tomb)
    if scenario == "retirement_fails":
        # a query that fails for a reason other than a missing table
        conn.execute("CREATE VIEW tombstone_components AS SELECT no_such_function(1) AS memory_id")
    else:
        conn.execute("CREATE TABLE tombstone_components (memory_id INTEGER)")
        conn.executemany("INSERT INTO tombstone_components VALUES (?)", comp)
    conn.commit()
    return conn


def _payload(conn, query):
    args = dict(p.split("=", 1) for p in query.lstrip("&").split("&") if p)
    body = payload.build_graph_payload(
        data._read_memory_rows(conn), data._read_crossref_rows(conn), data._read_retirement(conn),
        include_docs=args.get("docs") == "1", limit=int(args.get("limit", 2000)), min_score=0.40,
    )
    return json.loads(json.dumps(body))  # the wire form: JSON keys are strings, as in JS


def _strict_equal(a, b):
    """== that also tells true from 1 (Python's True == 1)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return (type(a) is bool) == (type(b) is bool) and a == b


def _defects(conn, query):
    args = dict(p.split("=", 1) for p in query.lstrip("&").split("&") if p)
    return payload.pages_defects(data._read_memory_rows(conn), include_docs=args.get("docs") == "1",
                                 limit=int(args.get("limit", 2000)))


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_payload_is_the_one_the_pages_code_returns(tmp_path, scenario, query):
    expected = json.loads(FIXTURE.read_text())[scenario][query]
    conn = build_store(tmp_path / "s.db", scenario)
    assert _defects(conn, query) == []  # an input Pages handles: parity is owed
    got = _payload(conn, query)
    for key in sorted(set(expected) | set(got)):
        assert key in got and key in expected, f"{key} only in {'pages' if key in expected else 'memora-all'}"
        assert _strict_equal(got[key], expected[key]), key
    assert list(got["nodes"][i]["id"] for i in range(len(got["nodes"]))) == [n["id"] for n in expected["nodes"]]


def test_created_at_is_ordered_by_utf16_code_units():
    fx = json.loads(FIXTURE.read_text())["unicode_times"][""]
    order = [n["id"] for n in fx["nodes"]]
    assert order.index(2) < order.index(1)  # U+FFFF sorts after U+1F600's D83D (newest first)
    assert fx["maxDate"] == "2026-09-30 \uffff" and order == [n["id"] for n in _payload(
        build_store(":memory:", "unicode_times"), "")["nodes"]]


@pytest.mark.parametrize("query", QUERIES)
def test_prototype_named_keys_throw_in_pages_and_are_mapped_correctly_here(query):
    """Pages defect (leader 8135): graph.ts throws; this port answers."""
    assert "__pages_throws__" in json.loads(FIXTURE.read_text())["prototype_keys"][query]
    conn = build_store(":memory:", "prototype_keys")
    assert _defects(conn, query) == [
        "#2 primary tag 'constructor'", "#2 tag 'constructor'", "#3 tag 'toString'", "#4 section 'hasOwnProperty'",
        "#5 component 'valueOf'", "#6 category '__proto__'", "#7 issue status 'isPrototypeOf'",
    ]
    got = _payload(conn, query)
    ids = {n["id"] for n in got["nodes"]}
    if 2 in ids:
        assert got["tagToNodes"]["constructor"] == [2] and got["tagColors"]["constructor"].startswith("#")
        # also a duplicate (0.9 to #1): the tag colour is the background
        assert next(n for n in got["nodes"] if n["id"] == 2)["color"]["background"] == got["tagColors"]["constructor"]
    if 4 in ids:
        assert got["sectionToNodes"]["hasOwnProperty"] == [4]
    if 5 in ids:
        assert got["issueCategoryToNodes"]["valueOf"] == [5]
    if 6 in ids:
        assert got["todoCategoryToNodes"]["__proto__"] == [6]
    if 7 in ids:
        assert got["statusToNodes"]["isPrototypeOf"] == [7]
        assert next(n for n in got["nodes"] if n["id"] == 7)["color"] == "#ff7b72"  # the open colour


@pytest.mark.parametrize("query", QUERIES)
def test_values_graph_ts_throws_on_are_named_and_answered_here(query):
    """Pages defect (leader 8135): each of these makes graph.ts throw."""
    assert "__pages_throws__" in json.loads(FIXTURE.read_text())["malformed_throws"][query]
    conn = build_store(":memory:", "malformed_throws")
    defects = _defects(conn, query)
    selected = {n["id"] for n in _payload(conn, query)["nodes"]}
    expected = ["#2 hierarchy.path str not sliceable", "#3 hierarchy.path dict not sliceable",
                "#4 subsection not a string", "#5 metadata null", "#6 tags null", "#7 tags not iterable",
                "#8 tags not iterable", "#9 tags null"]
    assert defects == [d for d in expected if int(d.split()[0][1:]) in selected]
    got = _payload(conn, query)
    if 2 in selected:  # the unsliceable path is treated as absent: no section from it
        assert "X" not in got["sectionToNodes"] and 2 in got["sectionToNodes"]["Uncategorized"]
    if 4 in selected:
        assert got["sectionToNodes"]["S"] == [4] and not any(k.startswith("S/") for k in got["subsectionToNodes"])


def test_a_prototype_named_fragment_tag_is_dropped_by_pages_and_kept_here():
    fx = json.loads(FIXTURE.read_text())["prototype_fragment"]
    conn = build_store(":memory:", "prototype_fragment")
    assert _defects(conn, "") == [] and _strict_equal(_payload(conn, ""), fx[""])  # not selected: parity
    assert _defects(conn, "&docs=1") == ["#3 primary tag 'constructor'"]
    got, pages = _payload(conn, "&docs=1"), fx["&docs=1"]
    assert "constructor" not in pages["tagColors"] and got["tagColors"]["constructor"] == payload.TAG_COLORS[1]
    assert _strict_equal({k: v for k, v in got.items() if k != "tagColors"},
                         {k: v for k, v in pages.items() if k != "tagColors"})


def test_the_fixture_covers_what_it_claims():
    fx = json.loads(FIXTURE.read_text())
    lin = fx["lineage"][""]
    assert {e["edge_type"] for e in lin["edges"]} >= {"supersedes", "references", "implements", "extends",
                                                       "contradicts", "related_to"}
    kinds = {c["kind"] for c in lin["lineageConflicts"]}
    assert kinds == {"self_cycle", "score_mismatch", "cycle"}
    assert lin["lineageDangling"] and lin["retiredIds"] and lin["duplicateIds"]
    assert any(n.get("authority_unknown") for n in lin["nodes"]) and any(n.get("superseded") for n in lin["nodes"])
    assert fx["corrupt_row"][""]["lineageDegradedReason"] == "corrupt_crossref:invalid_json"
    assert fx["no_crossrefs_table"][""]["lineageDegradedReason"] == "crossrefs_query_failed"
    assert fx["retirement_fails"][""]["lineageDegradedReason"] == "retirement_query_failed"
    docs = fx["docs"]["&docs=1"]
    assert any(e["edge_type"] == "document" for e in docs["edges"]) and any(n.get("frag") for n in docs["nodes"])
    content = fx["content"][""]
    assert {n["mass"] for n in content["nodes"]} >= {1.3788898309344877, 2.6112458636922073}  # c = 2, c = 13
    assert fx["content"]["&limit=12"]["truncated"] is True


@pytest.mark.parametrize("value, js", [
    (0, "0"), (5, "5"), (1.5, "1.5"), (1e21, "1e+21"), (1e-7, "1e-7"), (123456789012345680000, "123456789012345680000"),
    (0.1, "0.1"), (-2.5, "-2.5"), (1e-6, "0.000001"), (2 ** 53 + 2, "9007199254740994"), (float("inf"), "Infinity"),
    (True, "true"), (1.25e-10, "1.25e-10"), (3.0, "3"),
])
def test_numbers_become_strings_as_in_js(value, js):
    assert payload.js_number_str(value) == js


def test_an_over_long_json_integer_is_a_non_finite_number_not_an_error():
    """Review 8101 (P2): JSON.parse makes it Infinity, so the row is corrupt (a bad id)."""
    huge = "9" * 400
    assert payload.parse_related_payload(f'[{{"id": {huge}, "score": 0.5}}]') == (False, "entry_bad_id")
    ok, entries = payload.parse_related_payload(f'[{{"id": 5, "score": {huge}}}]')
    assert ok and entries[0]["score"] is payload.UNDEFINED  # a non-finite score is no score


def test_a_long_supersede_chain_needs_no_recursion():
    """Tarjan runs with an explicit stack: a 5000-long chain is fine."""
    refs = {i: [{"id": i + 1, "score": 1, "edge_type": "supersedes"}] for i in range(1, 5001)}
    maps = payload.build_lineage_maps(refs)
    assert len(maps.supersedes_edges) == 5000 and maps.conflicts == []


def test_a_label_cut_inside_a_surrogate_pair_is_served_as_an_escape(stores):
    from memora import storage
    from tests.test_g1_graph import _client

    reset = storage.CURRENT_DB.set("alpha")
    try:
        conn = storage.connect()
        try:
            storage.add_memory(conn, content="x" * 34 + "\U0001F600" + " after", tags=["g4"])
            conn.commit()
        finally:
            conn.close()
    finally:
        storage.CURRENT_DB.reset(reset)
    r = _client().get("/api/graph?db=alpha")
    assert r.status_code == 200 and "\\ud83d" in r.text
    labels = [n["label"] for n in r.json()["nodes"]]
    assert "x" * 34 + "\ud83d" in labels


def test_the_api_takes_docs_and_the_pages_edge_default(stores, monkeypatch):
    from memora.graph import server as gs
    from tests.test_g1_graph import _client

    seen = {}
    monkeypatch.setattr(gs, "get_graph_data", lambda *a, **k: seen.update(args=a, kwargs=k) or {"nodes": []})
    _client().get("/api/graph?db=alpha&docs=1")
    assert seen["args"][0] == 0.40 and seen["kwargs"]["include_docs"] is True
    _client().get("/api/graph?db=alpha&docs=true")
    assert seen["kwargs"]["include_docs"] is False  # only docs=1, as in Pages


def test_rows_of_an_unfinished_import_are_not_nodes(tmp_path):
    """The one deliberate difference from Pages: memora-all never lists
    import-pending rows (they are not memories yet)."""
    from memora.embeddings import IMPORT_MARKER_KEY

    conn = build_store(tmp_path / "s.db", "docs")
    conn.execute("UPDATE memories SET metadata = ? WHERE id = 14", (json.dumps({IMPORT_MARKER_KEY: "run-1"}),))
    ids = [r["id"] for r in data._read_memory_rows(conn)]
    assert 14 not in ids and 15 in ids


from tests.test_g1_graph import stores  # noqa: E402,F401  (fixture)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for s in SCENARIOS + DEFECT_SCENARIOS:
        (out / f"{s}.db").unlink(missing_ok=True)
        build_store(out / f"{s}.db", s).close()
        print(out / f"{s}.db")
