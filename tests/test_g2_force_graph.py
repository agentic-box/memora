"""G2: memora-all's graph server also serves the force-graph (2D/3D) view,
the SAME file the Pages build serves, at the same path (/force-graph.html,
plus /graph/force), behind the graph token, reading the per-store API."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_g1_graph import TOKEN, _client, _contents, stores  # noqa: F401  (fixture)

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ one source of truth

@pytest.mark.parametrize("name", ["force-graph.html", "_selection.mjs", "index.html", "_graph_limit.mjs"])
def test_the_pages_build_serves_the_same_file_as_memora_all(name):
    public = REPO / "memora-graph" / "public" / name
    assert public.is_symlink(), f"{public} must be a symlink to the packaged file, not a copy"
    assert os.path.realpath(public) == os.path.realpath(REPO / "memora" / "graph" / name)


def test_the_page_and_its_module_ship_in_the_package():
    text = (REPO / "pyproject.toml").read_text()
    assert '"force-graph.html"' in text and '"_selection.mjs"' in text


# ------------------------------------------------------------------ the routes

def test_the_page_loads_with_this_servers_config(stores):
    r = _client().get("/force-graph.html")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "window.MEMORA_CONFIG=" in r.text
    assert '"r2Prefix": "/r2/"' in r.text and '"dbSelector": true' in r.text
    assert "3d-force-graph" in r.text  # the vasturiano force-graph view itself


def test_graph_force_redirects_keeping_the_store(stores):
    r = _client().get("/graph/force?db=beta", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/force-graph.html?db=beta"


def test_its_modules_are_served(stores):
    c = _client()
    for path in ("/_selection.mjs", "/_graph_limit.mjs"):
        r = c.get(path)
        assert r.status_code == 200 and "javascript" in r.headers["content-type"], path
    assert "planSelectionReconcile" in c.get("/_selection.mjs").text


@pytest.mark.parametrize("path", ["/force-graph.html", "/graph/force", "/_selection.mjs"])
def test_every_new_route_needs_the_token(stores, path):
    c = _client(auth=False)
    r = c.get(path, follow_redirects=False)
    assert r.status_code == 401
    assert c.get(path, headers={"Authorization": "Bearer wrong"}, follow_redirects=False).status_code == 401


@pytest.mark.parametrize("path", ["/force-graph.html?db=beta", "/graph/force?db=beta"])
def test_an_unauthenticated_browser_gets_the_login_and_returns_to_the_view(stores, path):
    c = _client(auth=False)
    r = c.get(path, follow_redirects=False)
    assert r.status_code == 401 and 'action="/login"' in r.text and TOKEN not in r.text
    assert 'value="/graph/force?db=beta"' in r.text
    ok = c.post("/login", data={"token": TOKEN, "next": "/graph/force?db=beta"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/graph/force?db=beta"
    page = c.get("/graph/force?db=beta")  # the cookie opens it; the redirect lands on the view
    assert page.status_code == 200 and "3d-force-graph" in page.text


def test_the_view_reads_each_store_through_the_per_store_api(stores):
    """What the page fetches: /api/databases, /api/graph?db=, /api/memories?db=."""
    c = _client()
    assert c.get("/api/databases").json()["databases"] == ["alpha", "beta"]
    assert _contents(c.get("/api/memories?db=beta&sort=created&limit=200&offset=0")) == ["beta only"]
    labels = {n["label"] for n in c.get("/api/graph?db=alpha").json()["nodes"]}
    assert labels and all("beta" not in label for label in labels)


# ------------------------------------------------------------------ the page itself

def _page():
    return (REPO / "memora" / "graph" / "force-graph.html").read_text()


def test_the_page_has_no_edit_calls():
    """Read-only on both builds: no PATCH/POST/DELETE fetch at all, so the
    capabilities rule (Pages read-only, memora-all editable) has nothing to hide."""
    page = _page()
    for verb in ("PATCH", "DELETE", '"POST"', "'POST'"):
        assert verb not in page, verb


def test_the_image_prefix_comes_from_the_injected_config():
    page = _page()
    assert 'const R2_PREFIX = (window.MEMORA_CONFIG && window.MEMORA_CONFIG.r2Prefix) || "/api/r2/";' in page
    assert 'src = "/api/r2/" + ' not in page


def test_the_store_in_the_url_wins_over_the_stored_choice():
    assert 'new URLSearchParams(location.search).get("db") || storedDatabase()' in _page()


def test_the_main_viewer_links_to_it_with_its_store(stores):
    html = _client().get("/graph").text
    assert 'id="force-graph-link"' in html and "'/force-graph.html' + getDbParam()" in html
