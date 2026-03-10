import memora


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
