import asyncio

import memora.storage as storage
import memora.server as server


def _new_memory(*args, content="Repeat memory text", tags=["task"], **kwargs):
    return asyncio.run(
        server.memory_create(*args, content=content, tags=tags, **kwargs)
    )


def _raw_memory(*, content, tags, metadata=None):
    with storage.connect() as conn:
        return storage.add_memory(conn, content=content, tags=tags, metadata=metadata)


def test_memory_create_minimal_response_returns_id_only(local_db):
    r2 = _new_memory(content="Standalone memory", response_mode="minimal")

    assert r2 == {"memory": {"id": r2["memory"]["id"]}}


def test_memory_create_minimal_response_includes_similar_memory_info(local_db):
    _new_memory(content="Unique project memory for similarity coverage")
    response = _new_memory(
        content="Unique project memory for similarity coverage",
        response_mode="minimal",
    )

    assert response["memory"] == {"id": response["memory"]["id"]}
    assert response["similar_memories"]
    assert response["consolidation_hint"].startswith("Found 1 similar memories.")
    assert "warnings" in response
    assert set(response["warnings"]) == {"duplicate_warning"}


def test_memory_create_minimal_response_omits_similar_info_when_disabled(local_db):
    _new_memory(content="Another repeated memory")
    response = _new_memory(
        content="Another repeated memory",
        response_mode="minimal",
        suggest_similar=False,
    )

    assert response == {"memory": {"id": response["memory"]["id"]}}


def test_memory_digest_returns_deterministic_aggregation(local_db):
    old = _new_memory(
        content="Agent routing old design used manual pane addressing",
        tags=["clmux", "agent-routing"],
    )["memory"]
    current = _new_memory(
        content="Agent routing current design uses role-based MCP delivery",
        tags=["clmux", "agent-routing"],
    )["memory"]
    related = _new_memory(
        content="Worker registration keeps live agent role metadata available",
        tags=["clmux", "agent-routing"],
    )["memory"]
    todo = _new_memory(
        content="TODO: improve agent routing diagnostics",
        tags=["clmux", "agent-routing", "memora/todos"],
        metadata={"type": "todo", "status": "open"},
    )["memory"]
    issue = _new_memory(
        content="Issue: agent routing multiline injected asks may not wake panes",
        tags=["clmux", "agent-routing", "memora/issues"],
        metadata={"type": "issue", "status": "open"},
    )["memory"]
    issue_by_type = _raw_memory(
        content="Issue: agent routing prompt hang stored without issue tag",
        tags=["clmux", "agent-routing"],
        metadata={"type": "issue", "status": "open"},
    )
    todo_by_type = _raw_memory(
        content="Agent routing diagnostic followup stored without todo tag",
        tags=["clmux", "agent-routing"],
        metadata={"type": "todo", "status": "open"},
    )

    asyncio.run(server.memory_link(current["id"], old["id"], "supersedes"))
    asyncio.run(server.memory_link(current["id"], related["id"], "related_to"))

    digest = asyncio.run(server.memory_digest("agent routing", k=10))

    assert digest["topic"] == "agent routing"
    assert current["id"] in digest["memory_ids"]
    assert old["id"] not in digest["memory_ids"]
    assert any(
        chain["ids"] == [old["id"], current["id"]]
        for chain in digest["lineage_chains"]
    )
    assert related["id"] in digest["related_ids"]
    assert digest["todos"]
    assert digest["issues"]
    assert any(item["id"] == todo["id"] for item in digest["todos"])
    assert any(item["id"] == todo_by_type["id"] for item in digest["todos"])
    assert any(item["id"] == issue["id"] for item in digest["issues"])
    assert any(item["id"] == issue_by_type["id"] for item in digest["issues"])
    assert current["id"] in digest["source_ids"]
    assert old["id"] in digest["source_ids"]
    assert related["id"] in digest["source_ids"]


def test_memory_digest_filters_seeds_and_debug(local_db):
    filtered = _new_memory(
        content="Filtered digest target topic current plan",
        tags=["alpha", "digest"],
        metadata={"project": "target"},
    )["memory"]
    excluded = _new_memory(
        content="Filtered digest target topic excluded plan",
        tags=["beta", "digest"],
        metadata={"project": "other"},
    )["memory"]
    seed = _new_memory(
        content="Explicit seed unrelated to target query",
        tags=["seed-only"],
        metadata={"project": "seed"},
    )["memory"]
    related = _new_memory(
        content="Seed related expansion target",
        tags=["seed-only"],
    )["memory"]
    issue = _new_memory(
        content="Filtered digest target topic issue",
        tags=["alpha", "memora/issues"],
        metadata={"project": "target", "type": "issue", "status": "open"},
    )["memory"]
    other_issue = _new_memory(
        content="Filtered digest target topic other issue",
        tags=["beta", "memora/issues"],
        metadata={"project": "other", "type": "issue", "status": "open"},
    )["memory"]
    asyncio.run(server.memory_link(seed["id"], related["id"], "related_to"))

    digest = asyncio.run(
        server.memory_digest(
            "target topic",
            k=10,
            tags_all=["alpha"],
            metadata_filters={"project": "target"},
            seed_ids=[seed["id"], 999999],
            debug=True,
        )
    )

    assert filtered["id"] in digest["memory_ids"]
    assert excluded["id"] not in digest["memory_ids"]
    assert seed["id"] in digest["memory_ids"]
    assert related["id"] in digest["related_ids"]
    assert any(item["id"] == issue["id"] for item in digest["issues"])
    assert all(item["id"] != other_issue["id"] for item in digest["issues"])
    assert digest["parameters"]["tags_all"] == ["alpha"]
    assert digest["parameters"]["metadata_filters"] == {"project": "target"}
    assert digest["parameters"]["seed_ids"] == [seed["id"], 999999]
    assert digest["debug"]["missing_seed_ids"] == [999999]
    assert digest["debug"]["ranked_candidates"]
    assert "seed_ids are included explicitly" in digest["debug"]["filter_note"]


def test_memory_digest_synthesize_is_warning_only(local_db):
    _new_memory(content="Digest topic memory", tags=["digest"])

    digest = asyncio.run(server.memory_digest("digest topic", synthesize=True))

    assert "warnings" in digest
    assert digest["parameters"]["synthesize"] is True


def test_uniform_current_after_fork_collapse(local_db, monkeypatch):
    import memora.storage as st

    with st.connect() as conn:
        orig = st.add_memory(conn, content="Uniform current probe version one extra")
        left = st.add_memory(conn, content="Uniform current probe version two left extra")
        right = st.add_memory(conn, content="Uniform current probe version two right extra")
        st.add_link(conn, left["id"], orig["id"], edge_type="supersedes")
        st.add_link(conn, right["id"], orig["id"], edge_type="supersedes")
        monkeypatch.setattr(
            st,
            "_search_by_vector",
            lambda *a, **k: [{"score": 0.5, "memory": left}],
        )
        monkeypatch.setattr(
            st,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": left["id"],
                "relationship": "UPDATE",
                "reason": "v3",
            }], []),
        )
        monkeypatch.setattr(st, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        result = st.absorb_memory(conn, ["Uniform current probe version three extra"])
    new_id = next(d["memory_id"] for d in result["decisions"] if d["action"] == "superseded")

    got = asyncio.run(server.memory_get(orig["id"], follow="latest"))
    assert got["memory"]["id"] == new_id
    listed = asyncio.run(server.memory_list(query="Uniform current probe", follow="active", limit=50))
    list_ids = {m["id"] for m in listed.get("memories", [])}
    assert new_id in list_ids
    assert left["id"] not in list_ids and right["id"] not in list_ids
    searched = asyncio.run(
        server.memory_semantic_search("Uniform current probe", top_k=10, follow="latest")
    )
    search_ids = {(e.get("memory") or e)["id"] for e in searched.get("results", [])}
    assert new_id in search_ids
    assert left["id"] not in search_ids and right["id"] not in search_ids
    digest = asyncio.run(server.memory_digest("Uniform current probe", k=10, include_lineage=True))
    digest_ids = {m["id"] for m in digest.get("memories", [])}
    assert new_id in digest_ids
    assert left["id"] not in digest_ids and right["id"] not in digest_ids
    hist = asyncio.run(server.memory_get(new_id, follow="full_history"))
    hist_mem = hist.get("memory") or hist
    hist_ids = {m["id"] for m in hist_mem.get("history") or [hist_mem]}
    assert {orig["id"], left["id"], right["id"], new_id} <= hist_ids


def test_search_tools_honor_limit_alias_under_follow_active(local_db):
    """MCP callers pass ``limit`` (same as memory_list). Hybrid used to ignore it
    and return the top_k default of 10."""
    token = "limitprobe-zzxq"
    for i in range(12):
        _new_memory(content=f"{token} matching document number {i} extra context words here")
    _new_memory(content="completely unrelated office printer recycled paper")

    hybrid = asyncio.run(
        server.memory_hybrid_search(token, limit=3, follow="active", content_mode="full")
    )
    assert hybrid["count"] == 3, (
        f"hybrid limit=3 must return 3, got {hybrid['count']} "
        "(mutation: drop limit= from the tool signature and this goes red — default 10 leaks)"
    )
    assert len(hybrid["results"]) == 3

    semantic = asyncio.run(
        server.memory_semantic_search(token, limit=3, follow="active", content_mode="full")
    )
    assert semantic["count"] == 3, f"semantic limit=3 must return 3, got {semantic['count']}"
    assert len(semantic["results"]) == 3

    listed = asyncio.run(
        server.memory_list(query=token, limit=3, follow="active", content_mode="full")
    )
    assert listed["count"] == 3
    assert len(listed["memories"]) == 3

    # Legacy top_k still works on hybrid
    hybrid_topk = asyncio.run(
        server.memory_hybrid_search(token, top_k=4, follow="active")
    )
    assert hybrid_topk["count"] == 4

    rare = "qzxplmvw"
    _new_memory(content=f"{rare} first matching document extra words", tags=["rare-probe"])
    _new_memory(content=f"{rare} second matching document extra words", tags=["rare-probe"])
    few = asyncio.run(
        server.memory_hybrid_search(
            rare, limit=5, follow="active", tags_all=["rare-probe"]
        )
    )
    assert 1 <= few["count"] <= 2
    assert few["count"] == len(few["results"])
    assert few["count"] < 5


def test_resolve_search_cap_prefers_limit_over_top_k_default():
    assert server._resolve_search_cap(limit=3, top_k=None, default=10) == 3
    assert server._resolve_search_cap(limit=3, top_k=10, default=10) == 3
    assert server._resolve_search_cap(limit=None, top_k=4, default=10) == 4
    assert server._resolve_search_cap(limit=None, top_k=None, default=10) == 10


def test_default_semantic_search_excludes_superseded_memory(local_db):
    """Default list/search follow=active: superseded memories stay out of ordinary recall.

    Explicit follow=\"all\" is the forensic escape hatch (omitting follow is NOT unfiltered).
    """
    old = _new_memory(
        content="Unique lineage probe alpha-routing pane-name derived role policy",
        tags=["lineage-probe", "clmux"],
    )["memory"]
    current = _new_memory(
        content="Unique lineage probe alpha-routing registry role is identity not pane name",
        tags=["lineage-probe", "clmux"],
    )["memory"]
    asyncio.run(server.memory_link(current["id"], old["id"], "supersedes"))

    # Default semantic search (follow omitted → active)
    default = asyncio.run(
        server.memory_semantic_search(
            "Unique lineage probe alpha-routing",
            top_k=10,
            content_mode="full",
        )
    )
    default_ids = {
        (entry.get("memory") or entry)["id"]
        for entry in default.get("results", [])
    }
    assert current["id"] in default_ids
    assert old["id"] not in default_ids, (
        f"superseded memory #{old['id']} must not appear under default semantic_search; got {default_ids}"
    )

    # Explicit unfiltered history
    unfiltered = asyncio.run(
        server.memory_semantic_search(
            "Unique lineage probe alpha-routing",
            top_k=10,
            content_mode="full",
            follow="all",
        )
    )
    unfiltered_ids = {
        (entry.get("memory") or entry)["id"]
        for entry in unfiltered.get("results", [])
    }
    assert old["id"] in unfiltered_ids, (
        f"follow='all' must return superseded memory #{old['id']}; got {unfiltered_ids}"
    )

    # Default list also excludes superseded
    listed = asyncio.run(
        server.memory_list(query="Unique lineage probe alpha-routing", limit=-1, content_mode="full")
    )
    list_ids = {m["id"] for m in listed.get("memories", [])}
    assert current["id"] in list_ids
    assert old["id"] not in list_ids


def test_default_memory_get_resolves_superseded_id_to_latest(local_db):
    """Default memory_get follow=latest: fetch by superseded id returns the current leaf."""
    old = _new_memory(
        content="Lineage get-by-id obsolete zig@0.14 build path instruction",
        tags=["lineage-get"],
    )["memory"]
    current = _new_memory(
        content="Lineage get-by-id current zig 0.15 default toolchain instruction",
        tags=["lineage-get"],
    )["memory"]
    asyncio.run(server.memory_link(current["id"], old["id"], "supersedes"))

    resolved = asyncio.run(server.memory_get(old["id"]))
    assert "error" not in resolved, resolved
    resolved_mem = resolved.get("memory") or resolved
    assert resolved_mem["id"] == current["id"], (
        f"default get({old['id']}) should resolve to #{current['id']}, got #{resolved_mem.get('id')}"
    )

    # Forensic escape: exact id, no walk
    exact = asyncio.run(server.memory_get(old["id"], follow="all"))
    exact_mem = exact.get("memory") or exact
    assert exact_mem["id"] == old["id"]


def test_document_get_still_returns_superseded_roots(local_db):
    """server memory_get_document calls _list_memories without follow — must keep superseded roots.

    If follow=\"active\" were applied here, historical version=N retrieval would break.
    """
    key = "test/lineage-doc-version-probe"
    v1 = _raw_memory(
        content="Document root version 1 historical body",
        tags=["doc-lineage"],
        metadata={"type": "document_root", "document_key": key, "document_version": 1},
    )
    v2 = _raw_memory(
        content="Document root version 2 current body",
        tags=["doc-lineage"],
        metadata={"type": "document_root", "document_key": key, "document_version": 2},
    )
    asyncio.run(server.memory_link(v2["id"], v1["id"], "supersedes"))

    # Prove v1 is superseded for active list, but document path still finds it by version.
    listed = asyncio.run(
        server.memory_list(
            metadata_filters={"document_key": key, "type": "document_root"},
            limit=-1,
            content_mode="full",
        )
    )
    list_ids = {m["id"] for m in listed.get("memories", [])}
    assert v2["id"] in list_ids
    assert v1["id"] not in list_ids, "active list must hide superseded document root"

    historical = asyncio.run(server.memory_get_document(document_key=key, version=1))
    assert "error" not in historical, historical
    assert historical["root"]["id"] == v1["id"], (
        f"version=1 must return superseded root #{v1['id']}, got {historical.get('root', {}).get('id')}"
    )
    assert historical["version"] == 1

    latest = asyncio.run(server.memory_get_document(document_key=key))
    assert latest["root"]["id"] == v2["id"]
    assert latest["version"] == 2
