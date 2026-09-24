"""Slice L7: the viewer is read-only (docs/local-primary-implementation.md
§6 F1/F2). The Pages functions are covered by the D1 write guard and by
memora-graph/scripts/test_readonly.mjs; this file covers the shared page
(memora/graph/index.html, served both by Pages and by memora's own graph
server) and the capability that decides whether its edit controls show."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = (REPO / "memora" / "graph" / "index.html").read_text(encoding="utf-8")
FORCE = (REPO / "memora-graph" / "public" / "force-graph.html").read_text(encoding="utf-8")

# Every function in index.html that can send a write.
WRITERS = ("patchMemory", "enterPanelEditMode", "savePanelEdits", "changeIssueStatus",
           "changeIssueCategory", "toggleFavorite")


def _body(src, name):
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"{name} not found"
    return src[m.end(): m.end() + 400]


def test_the_page_starts_read_only_and_fails_closed():
    assert '<body class="read-only">' in INDEX
    assert "var viewerReadOnly = true;" in INDEX
    # Only an explicit read_only === false from the server enables edits;
    # a failed or unknown answer stays read-only.
    assert "c.read_only === false" in INDEX
    assert re.search(r"\.catch\(function\(\)\s*\{\s*viewerReadOnly = true;", INDEX)
    assert "Read-only viewer" in INDEX


def test_every_write_function_refuses_while_read_only():
    for name in WRITERS:
        first = _body(INDEX, name).lstrip().splitlines()[0]
        assert first.startswith("if (viewerReadOnly)"), f"{name} is not guarded: {first!r}"


def test_every_patch_is_inside_a_guarded_function():
    """No PATCH fetch outside the guarded functions (a new edit path must be
    guarded too)."""
    for m in re.finditer(r"method:\s*'PATCH'", INDEX):
        before = INDEX[: m.start()]
        fn = re.findall(r"function\s+(\w+)\s*\(", before)[-1]
        assert fn in WRITERS, f"PATCH at offset {m.start()} is in unguarded function {fn}"


def test_edit_controls_are_hidden_or_inert_in_read_only_mode():
    for sel in ("#panel-edit-btn", "#panel-save-btn", "#panel-cancel-btn"):
        assert re.search(r"body\.read-only " + re.escape(sel) + r"[\s,{]", INDEX), sel
    assert re.search(r"body\.read-only \.favorite-star,\s*body\.read-only \.issue-select \{ pointer-events: none;", INDEX)


def test_force_graph_has_no_favorite_write():
    assert "PATCH" not in FORCE and "toggleFavorite" not in FORCE
    assert "Read-only" in FORCE


def test_local_graph_server_declares_itself_writable(graph_request):
    status, body = graph_request("GET", "/api/capabilities")
    assert status == 200 and body["read_only"] is False  # G1 adds the selected db and its gate state


def test_pages_capabilities_declares_read_only():
    src = (REPO / "memora-graph" / "functions" / "api" / "capabilities.ts").read_text()
    assert "read_only: true" in src and "onRequestGet" in src
    assert not re.search(r"onRequest(Post|Put|Patch|Delete)", src)
