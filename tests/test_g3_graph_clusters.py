"""G3: memora-all's graph API clusters exactly as the Pages viewer does
(functions/api/graph.ts buildClusterData / louvainCommunities): Louvain over
the STORED crossrefs (score >= 0.4) of the graph's own nodes -- not over
all-pairs embedding similarity of every memory.

tests/fixtures/g3_pages_clusters.json holds the clusters the REAL Pages code
computed for each scenario store below (esbuild bundle of graph.ts over the
SQLite file through a D1 shim). Regenerate with
``python -m tests.test_g3_graph_clusters OUTDIR`` (writes the stores), then
run the bundle on each and record clusterToNodes/clusterColors/clusterMeta.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

from memora.graph import data
from tests.test_g1_graph import stores  # noqa: F401  (fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "g3_pages_clusters.json"
SCENARIOS = ["random", "all_weak", "ring_ties", "odd_entries", "corrupt_row", "at_threshold"]


def _memories(n, sections=()):
    rows = []
    for i in range(1, n + 1):
        meta = {"type": "section"} if i in sections else {}
        # a few equal timestamps so the id tie-break decides the node order
        created = f"2026-09-{1 + (i * 7) % 20:02d} 10:00:{(i % 3):02d}"
        rows.append((i, f"memory {i}", json.dumps(meta), json.dumps([f"t{i % 4}"]), created, created))
    return rows


def _crossrefs(scenario):
    rnd = random.Random(scenario)
    refs = {}
    if scenario == "random":
        n = 150
        for i in range(1, n + 1):
            out = []
            for _ in range(rnd.randrange(0, 9)):
                j = rnd.randrange(1, n + 1)
                block = (i // 15) == (j // 15)
                score = round(rnd.uniform(0.35, 0.95) if block else rnd.uniform(0.2, 0.6), 6)
                entry = {"id": j, "score": score}
                if rnd.random() < 0.2:
                    entry["edge_type"] = rnd.choice(["related_to", "supersedes", "contradicts"])
                out.append(entry)
            refs[i] = json.dumps(out)
        return n, {5, 77}, refs
    if scenario == "all_weak":
        n = 12
        for i in range(1, n + 1):
            refs[i] = json.dumps([{"id": (i % n) + 1, "score": 0.39}])
        return n, set(), refs
    if scenario == "ring_ties":
        n = 24
        for i in range(1, n + 1):
            refs[i] = json.dumps([{"id": (i % n) + 1, "score": 0.5}, {"id": ((i + 1) % n) + 1, "score": 0.5}])
        return n, set(), refs
    if scenario == "at_threshold":
        n = 12
        for i in range(1, n + 1):
            refs[i] = json.dumps([{"id": (i % n) + 1, "score": 0.39}])
        for group, score in (((1, 2, 3), 0.4), ((4, 5, 6), True), ((7, 8, 9), None)):  # exactly 0.4 / bool / none
            for i in group:
                refs[i] = json.dumps([{"id": o, "score": score} if score is not None else {"id": o}
                                      for o in group if o != i])
        return n, set(), refs
    if scenario in ("odd_entries", "corrupt_row"):
        n = 40
        for i in range(1, n + 1):
            out = [{"id": j, "score": 0.8} for j in range(1 + 8 * ((i - 1) // 8), 9 + 8 * ((i - 1) // 8)) if j != i]
            refs[i] = json.dumps(out)
        refs[20] = json.dumps([{"id": 21}, {"id": 22, "score": True}, {"id": 18, "score": 1},
                               {"id": 19, "score": "0.9"}, {"id": 17, "score": 0.9, "edge_type": 7}])  # no/bool/int/str score
        refs[25] = json.dumps([{"id": 25, "score": 0.9}, {"id": 999, "score": 0.9}, {"id": 26, "score": 0.9}])  # self, unknown
        refs[27] = json.dumps([{"id": 27.0, "score": 0.9}, {"id": 28, "score": 0.9}, {"id": 28, "score": 0.45}])  # float id, repeat
        refs[30] = ""
        refs[31] = None
        refs[36] = json.dumps([{"id": 33, "score": 0.9}])  # to a section: not a node
        if scenario == "corrupt_row":
            refs[3] = "not json"  # one corrupt row: Pages emits no clusters at all
        return n, {33}, refs
    raise ValueError(scenario)


def build_store(path, scenario):
    n, sections, refs = _crossrefs(scenario)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, metadata TEXT, tags TEXT,"
                 " created_at TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE memories_crossrefs (memory_id INTEGER PRIMARY KEY, related TEXT)")
    conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?)", _memories(n, sections))
    conn.executemany("INSERT INTO memories_crossrefs VALUES (?,?)", sorted(refs.items(), reverse=True))
    conn.commit()
    return conn


def _node_ids(conn):
    """The Pages node order: non-section memories, created_at DESC, id DESC."""
    rows = conn.execute("SELECT id, metadata, created_at FROM memories").fetchall()
    rows = [r for r in rows if json.loads(r[1]).get("type") != "section"]
    rows.sort(key=lambda r: (r[2] or "", r[0]), reverse=True)
    return [r[0] for r in rows]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_clusters_are_those_the_pages_code_computes(tmp_path, scenario):
    expected = json.loads(FIXTURE.read_text())[scenario]
    conn = build_store(tmp_path / "s.db", scenario)
    got = data._build_cluster_data(data._load_crossrefs_map(conn), _node_ids(conn))
    for key in ("clusterToNodes", "clusterColors", "clusterMeta"):
        assert got[key] == expected[key], key
        assert list(got[key]) == list(expected[key]), f"{key}: key order (colours follow it)"
    assert got["nodeToCluster"] == {str(m): int(c) for c, ms in expected["clusterToNodes"].items() for m in ms}


def test_the_fixture_is_not_trivial():
    fx = json.loads(FIXTURE.read_text())
    assert len(fx["random"]["clusterToNodes"]) >= 5
    assert len(fx["all_weak"]["clusterToNodes"]) == 12  # Pages: no edge >= 0.4 -> each node its own cluster
    assert len(fx["odd_entries"]["clusterToNodes"]) >= 4
    assert fx["corrupt_row"]["clusterToNodes"] == {}
    assert sorted(sorted(v) for v in fx["at_threshold"]["clusterToNodes"].values()) == [[1, 2, 3]]


def _louvain_literal(adj, min_size=3):
    """graph.ts louvainCommunities transcribed line by line: every
    community total recomputed over all nodes before each node's move."""
    node_list = list(adj)
    community = {n: n for n in node_list}
    m2 = 0
    for nb in adj.values():
        for w in nb.values():
            m2 += w
    if m2 == 0:
        return community
    strength = {}
    for n in node_list:
        t = 0
        for w in adj[n].values():
            t += w
        strength[n] = t
    improved, iterations = True, 0
    while improved and iterations < 50:
        improved = False
        iterations += 1
        for node in node_list:
            cur, ki = community[node], strength[node]
            cw = {}
            for nbr, w in adj[node].items():
                cw[community[nbr]] = cw.get(community[nbr], 0) + w
            tot = {}
            for n in node_list:
                tot[community[n]] = tot.get(community[n], 0) + strength[n]
            ki_in = cw.get(cur, 0)
            sigma = tot[cur] - ki
            loss = ki_in / m2 - (sigma * ki) / (m2 * m2)
            best_gain, best = 0, cur
            for c, kt in cw.items():
                if c == cur:
                    continue
                gain = kt / m2 - (tot.get(c, 0) * ki) / (m2 * m2) - loss
                if gain > best_gain:
                    best_gain, best = gain, c
            if best != cur:
                community[node] = best
                improved = True
    uniq = list(dict.fromkeys(community.values()))
    remap = {}
    for c in uniq:
        if sum(1 for v in community.values() if v == c) >= min_size:
            remap[c] = len(remap)
    return {n: remap[c] for n, c in community.items() if c in remap}


def test_the_port_matches_the_literal_transcription_on_many_graphs():
    """Tie-heavy weights (a few repeated values whose sums round
    differently by order) over many random graphs and node orders."""
    rnd = random.Random(1)
    weights = [0.1, 0.2, 0.3, 0.7, 0.6, 0.45, 0.4]
    for trial in range(1000):  # this sequence first tells member order apart (summation order) at trial 652
        n = rnd.randrange(4, 30)
        nodes = rnd.sample(range(1, 200), n)
        adj = {v: {} for v in nodes}
        for _e in range(rnd.randrange(n, 4 * n)):
            a, b = rnd.choice(nodes), rnd.choice(nodes)
            if a == b:
                continue
            w = rnd.choice(weights)
            adj[a][b] = w
            adj[b][a] = w
        size = 1 if trial % 2 == 0 else 3
        assert data._louvain_communities_pages(adj, size) == _louvain_literal(adj, size)


@pytest.mark.parametrize("related", ["not json", '{"id": 5}', '[{"id": 5, "score": 0.9}, "x"]',
                                     '[{"score": 0.9}]', '[{"id": "5", "score": 0.9}]',
                                     '[{"id": 5, "score": NaN}]', '[{"id": 5, "score": Infinity}]',
                                     '[{"id": 5, "score": -Infinity}]'])
def test_a_corrupt_row_makes_the_crossrefs_unavailable(related):
    conn = build_store(":memory:", "odd_entries")
    conn.execute("UPDATE memories_crossrefs SET related = ? WHERE memory_id = 3", (related,))
    refs, available = data._load_crossrefs_map(conn)
    assert not available and 3 not in refs and 4 in refs
    assert data._build_cluster_data((refs, available), _node_ids(conn))["clusterToNodes"] == {}


def test_the_graph_api_clusters_over_crossrefs_not_embeddings(monkeypatch):
    """No all-pairs similarity pass on the graph path (the old cause of both
    the wrong clusters and the O(n^2) time)."""
    import memora.storage as storage

    def boom(*a, **k):
        raise AssertionError("the graph must not build the all-pairs similarity graph")
    monkeypatch.setattr(storage, "_build_similarity_graph", boom)
    monkeypatch.setattr(storage, "detect_clusters", boom)
    assert not hasattr(data, "detect_clusters")
    conn = build_store(":memory:", "odd_entries")
    assert data._build_cluster_data(data._load_crossrefs_map(conn), _node_ids(conn))["clusterToNodes"]



def test_the_graph_api_serves_those_clusters_end_to_end(stores):
    """Through /api/graph on a real store: its nodes, its stored crossrefs."""
    from memora import storage
    from tests.test_g1_graph import _client

    reset = storage.CURRENT_DB.set("alpha")
    try:
        conn = storage.connect()
        try:
            ids = [storage.add_memory(conn, content=f"topic {i // 4} note {i}", tags=["g3"])["id"] for i in range(12)]
            section = storage.add_memory(conn, content="a section", tags=["g3"], metadata={"type": "section"})["id"]
            conn.execute("DELETE FROM memories_crossrefs")
            for i, mid in enumerate(ids):
                group = ids[4 * (i // 4): 4 * (i // 4) + 4]
                refs = [{"id": o, "score": 0.8, "edge_type": "related_to"} for o in group if o != mid]
                refs.append({"id": section, "score": 0.99})  # not a node: never clusters anything
                refs.append({"id": ids[(i + 4) % 12], "score": 0.39})  # below 0.4: not in the cluster graph
                conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (?, ?)", (mid, json.dumps(refs)))
            conn.commit()
        finally:
            conn.close()
    finally:
        storage.CURRENT_DB.reset(reset)

    body = _client().get("/api/graph?db=alpha").json()
    groups = sorted(sorted(v) for v in body["clusterToNodes"].values())
    assert groups == [sorted(ids[0:4]), sorted(ids[4:8]), sorted(ids[8:12])]
    assert sorted(m["label"] for m in body["clusterMeta"].values()) == ["Cluster 1", "Cluster 2", "Cluster 3"]
    assert set(body["clusterColors"]) == set(body["clusterToNodes"])
    assert section not in {m for members in body["clusterToNodes"].values() for m in members}
    assert "nodeToCluster" not in body  # G4: the payload is exactly Pages' (it has none)

if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for s in SCENARIOS:
        (out / f"{s}.db").unlink(missing_ok=True)
        build_store(out / f"{s}.db", s).close()
        print(out / f"{s}.db")
