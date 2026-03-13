import asyncio

import memora.server as server


def _new_memory(*args, content="Repeat memory text", tags=["task"], **kwargs):
    return asyncio.run(
        server.memory_create(*args, content=content, tags=tags, **kwargs)
    )


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
