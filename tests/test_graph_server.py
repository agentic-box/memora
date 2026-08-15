import memora
import memora.storage as storage
from memora.graph.data import get_graph_data


def test_graph_retired_ancestor_not_current(graph_request, local_db):
    """A<-B, delete B: A must not appear current in graph or timeline payload."""
    with storage.connect() as conn:
        ancestor = storage.add_memory(conn, content="Retired graph ancestor extra")
        leaf = storage.add_memory(conn, content="Retired graph leaf extra")
        storage.add_link(conn, leaf["id"], ancestor["id"], edge_type="supersedes")
        storage.delete_memory(conn, leaf["id"])
        ancestor_id = ancestor["id"]

    data = get_graph_data()
    node = next(n for n in data["nodes"] if n["id"] == ancestor_id)
    assert node.get("retired") or node.get("authority_unknown"), (
        "mutation: local producer ignores tombstones; ancestor looks current"
    )
    current_only = [
        n for n in data["nodes"]
        if not n.get("superseded") and not n.get("authority_unknown")
    ]
    assert ancestor_id not in {n["id"] for n in current_only}

    status, api = graph_request("GET", "/api/graph")
    assert status == 200
    api_node = next(n for n in api["nodes"] if n["id"] == ancestor_id)
    assert api_node.get("retired") or api_node.get("authority_unknown")
    timeline_current = [
        n for n in api["nodes"]
        if not n.get("superseded") and not n.get("authority_unknown")
    ]
    assert ancestor_id not in {n["id"] for n in timeline_current}


def test_graph_patch_updates_tags_and_metadata(graph_request, memory_factory):
    created = memory_factory(metadata={"priority": "low", "favorite": False})

    status, data = graph_request(
        "PATCH",
        f"/api/memories/{created['id']}",
        {"tags": ["beta"], "metadata": {"priority": "high", "favorite": True}},
    )

    assert status == 200
    assert data["id"] == created["id"]
    assert data["tags"] == ["beta"]
    assert data["metadata"]["priority"] == "high"
    assert data["metadata"]["favorite"] is True
    assert data["updated"]


def test_graph_patch_supports_favorite_compatibility(graph_request, memory_factory):
    created = memory_factory(
        content="Favorite memory",
        metadata={"note": "keep"},
    )

    status, data = graph_request(
        "PATCH",
        f"/api/memories/{created['id']}",
        {"favorite": True},
    )

    assert status == 200
    assert data["metadata"]["favorite"] is True
    assert data["metadata"]["note"] == "keep"
    assert data["tags"] == ["alpha"]


def test_graph_patch_missing_memory_returns_404(graph_request):
    status, data = graph_request(
        "PATCH",
        "/api/memories/999999",
        {"tags": ["alpha"], "metadata": {}},
    )

    assert status == 404
    assert data["error"] == "not_found"


def test_graph_patch_rejects_invalid_tags_against_whitelist(
    graph_request, memory_factory, monkeypatch
):
    monkeypatch.setattr(memora, "TAG_WHITELIST", {"allowed"})
    created = memory_factory(content="Whitelist memory", tags=["allowed"])

    status, data = graph_request(
        "PATCH",
        f"/api/memories/{created['id']}",
        {"tags": ["forbidden"], "metadata": {}},
    )

    assert status == 400
    assert "Tag" in data["error"]


def test_patch_metadata_merges_keys(graph_request, memory_factory):
    """PATCH with partial metadata should preserve existing keys."""
    created = memory_factory(metadata={"existing_key": "keep", "section": "docs"})

    status, data = graph_request(
        "PATCH",
        f"/api/memories/{created['id']}",
        {"metadata": {"new_key": "added"}},
    )

    assert status == 200
    assert data["metadata"]["existing_key"] == "keep"
    assert data["metadata"]["section"] == "docs"
    assert data["metadata"]["new_key"] == "added"


def test_patch_metadata_null_deletes_key(graph_request, memory_factory):
    """PATCH with null value should delete that metadata key."""
    created = memory_factory(metadata={"to_remove": "bye", "to_keep": "stay"})

    status, data = graph_request(
        "PATCH",
        f"/api/memories/{created['id']}",
        {"metadata": {"to_remove": None}},
    )

    assert status == 200
    assert "to_remove" not in data["metadata"]
    assert data["metadata"]["to_keep"] == "stay"


def test_patch_preserves_favorite(graph_request, memory_factory):
    """PATCH metadata should preserve favorite field."""
    created = memory_factory(metadata={"note": "test"})

    # Set favorite via compatibility field
    graph_request("PATCH", f"/api/memories/{created['id']}", {"favorite": True})

    # Patch metadata without touching favorite
    status, data = graph_request(
        "PATCH",
        f"/api/memories/{created['id']}",
        {"metadata": {"note": "updated"}},
    )

    assert status == 200
    assert data["metadata"]["favorite"] is True
    assert data["metadata"]["note"] == "updated"


def test_graph_limit_returns_newest_subset(graph_request, memory_factory):
    """?limit=N keeps only the N newest memories, newest first, and reports
    truncation + total."""
    for i in range(5):
        memory_factory(content=f"Limit memory {i}")

    status, data = graph_request("GET", "/api/graph?limit=3")

    assert status == 200
    assert data["truncated"] is True
    assert data["total"] == 5
    # Newest first: all memories share created_at (same tick), so id DESC.
    assert len(data["nodes"]) == 3
    assert [n["id"] for n in data["nodes"]] == [5, 4, 3]


def test_graph_limit_within_total_not_truncated(graph_request, memory_factory):
    """?limit=N larger than the node count is not truncated."""
    for i in range(3):
        memory_factory(content=f"Limit memory {i}")

    status, data = graph_request("GET", "/api/graph?limit=10")

    assert status == 200
    assert data["truncated"] is False
    assert data["total"] == 3
    assert len(data["nodes"]) == 3


def test_graph_limit_default_unbounded(graph_request, memory_factory):
    """No ?limit= returns the full graph (below the default cap) untruncated."""
    for i in range(4):
        memory_factory(content=f"Limit memory {i}")

    status, data = graph_request("GET", "/api/graph")

    assert status == 200
    assert data["truncated"] is False
    assert len(data["nodes"]) == 4


def test_graph_default_cap_truncates(monkeypatch, graph_request, memory_factory):
    """Without ?limit=, a store above the default cap is truncated.

    Monkeypatch the default cap down to 2 so the test needs few memories
    (mutation: removing the default cap returns all and truncated=false).
    """
    import memora.graph.data as graph_data

    monkeypatch.setattr(graph_data, "DEFAULT_GRAPH_LIMIT", 2)
    for i in range(5):
        memory_factory(content=f"Limit memory {i}")

    status, data = graph_request("GET", "/api/graph")

    assert status == 200
    assert data["truncated"] is True
    assert data["total"] == 5
    assert len(data["nodes"]) == 2
    # Newest first (same created_at tick, id DESC): the two newest ids.
    assert [n["id"] for n in data["nodes"]] == [5, 4]


def test_graph_limit_invalid_rejected(graph_request, memory_factory):
    """Non-numeric, zero, and negative ?limit values return 400 invalid_limit."""
    memory_factory(content="Limit memory")

    for bad in ("abc", "0", "-5", "2.5"):
        status, data = graph_request("GET", f"/api/graph?limit={bad}")
        assert status == 400, f"limit={bad!r} should be 400"
        assert data["error"] == "invalid_limit", f"limit={bad!r} error code"


def test_graph_limit_clamps_to_hard_max(monkeypatch, graph_request, memory_factory):
    """?limit= above the hard cap is clamped, not rejected.

    Monkeypatch GRAPH_LIMIT_MAX down to 3 so the clamp is observable on a small
    store (mutation: removing the clamp / raising the max returns all 5 nodes
    untruncated).
    """
    import memora.graph.data as graph_data

    monkeypatch.setattr(graph_data, "GRAPH_LIMIT_MAX", 3)
    for i in range(5):
        memory_factory(content=f"Limit memory {i}")

    status, data = graph_request("GET", "/api/graph?limit=999999")

    assert status == 200
    assert len(data["nodes"]) == 3
    assert data["truncated"] is True
    assert data["total"] == 5
    # Clamp keeps the newest (same created_at tick, id DESC).
    assert [n["id"] for n in data["nodes"]] == [5, 4, 3]


def test_graph_limit_no_dangling_edges(graph_request, local_db):
    """Edges must never reference a node that was excluded by ?limit."""
    with storage.connect() as conn:
        a = storage.add_memory(conn, content="Limit edge A")
        b = storage.add_memory(conn, content="Limit edge B")
        storage.add_link(conn, a["id"], b["id"], edge_type="references")

    # limit=1 keeps only the newest node; the reference edge to the excluded
    # node must be dropped so no edge dangles.
    status, data = graph_request("GET", "/api/graph?limit=1")

    assert status == 200
    assert len(data["nodes"]) == 1
    node_ids = {n["id"] for n in data["nodes"]}
    for edge in data["edges"]:
        assert edge["from"] in node_ids and edge["to"] in node_ids, (
            "mutation: edge dangles to an excluded node"
        )


def test_graph_limit_clamp_observable_on_volume(local_db):
    """Clamp is observable against the real GRAPH_LIMIT_MAX.

    Seed a store larger than GRAPH_LIMIT_MAX, request above it, and assert
    exactly GRAPH_LIMIT_MAX nodes + truncated + total (mutation: raising
    GRAPH_LIMIT_MAX returns all rows untruncated).
    """
    from memora.graph.data import GRAPH_LIMIT_MAX, get_graph_data

    # Fixed row count (5100 > the real GRAPH_LIMIT_MAX of 5000), independent of
    # the constant so a mutation that raises GRAPH_LIMIT_MAX doesn't blow up the
    # insert loop and the clamp stays observable.
    n = 5100
    with storage.connect() as conn:
        for i in range(1, n + 1):
            conn.execute(
                "INSERT INTO memories (id, content, metadata, tags, created_at, updated_at) "
                "VALUES (?, ?, '{}', '[]', '2026-01-01T00:00:00Z', NULL)",
                (i, f"bulk {i}"),
            )

    data = get_graph_data(limit=GRAPH_LIMIT_MAX + 999)

    assert data["truncated"] is True
    assert data["total"] == n
    assert len(data["nodes"]) == GRAPH_LIMIT_MAX
