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
