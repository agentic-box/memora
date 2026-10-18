"""Item 104: deleting any version retires the whole supersession lineage (the tested invariant); the retired
id says so, naming the delete, and a new supersedes edge does not restore it."""

import asyncio

import memora.server as server
import memora.storage as storage


def _chain(conn, n):
    ids = [storage.add_memory(conn, content=f"Lineage fact version {i} extra words", tags=["t"])["id"] for i in range(n)]
    for older, newer in zip(ids, ids[1:]):
        storage.add_link(conn, newer, older, edge_type="supersedes")
    return ids


def test_deleting_the_leaf_retires_the_predecessor_and_names_the_delete(local_db):
    with storage.connect() as conn:
        a, b = _chain(conn, 2)
        assert storage.delete_memory(conn, b) is True
        note = storage.retirement_note(conn, a)
    assert note["deleted_id"] == b and f"lineage of #{b}" in note["reason"]
    got = asyncio.run(server.memory_get(a))                      # default follow=latest
    assert got["error"] == "not_found" and got["retired"] is True
    assert f"deleting #{b}" in got["message"] and "restore" in got["message"] and 'follow="all"' in got["message"]
    assert asyncio.run(server.memory_get(a, follow="all"))["memory"]["id"] == a   # forensic read still finds it


def test_a_new_supersedes_edge_to_a_retired_id_does_not_restore_it(local_db):
    with storage.connect() as conn:
        a, b = _chain(conn, 2)
        storage.delete_memory(conn, b)
        c = storage.add_memory(conn, content="Replacement fact extra words", tags=["t"])["id"]
        storage.add_link(conn, c, a, edge_type="supersedes")        # the 158 -> 149 edge of the report
        assert storage.get_memory(conn, a, follow="latest") is None
        assert storage.retirement_note(conn, a)["deleted_id"] == b


def test_a_delete_in_a_forked_chain_retires_every_branch_and_each_names_the_delete(local_db):
    with storage.connect() as conn:
        root, left = _chain(conn, 2)
        right = storage.add_memory(conn, content="Lineage fork right extra words", tags=["t"])["id"]
        storage.add_link(conn, right, root, edge_type="supersedes")
        assert storage.delete_memory(conn, left) is True
        for mid in (root, right):
            assert storage.get_memory(conn, mid, follow="latest") is None
            assert storage.retirement_note(conn, mid)["deleted_id"] == left
        assert storage.get_memory(conn, right) is not None          # follow=None (all) still reads it
        active = {m["id"] for m in storage.list_memories(conn, follow="active")}
        assert not {root, left, right} & active


def test_an_unrelated_or_live_memory_has_no_retirement_note(local_db):
    with storage.connect() as conn:
        a, b = _chain(conn, 2)
        other = storage.add_memory(conn, content="Unrelated live memory extra words", tags=["t"])["id"]
        assert storage.retirement_note(conn, other) is None and storage.retirement_note(conn, b) is None
        assert storage.retirement_note(conn, 99999) is None
    assert asyncio.run(server.memory_get(99999)) == {"error": "not_found", "id": 99999}


def test_a_row_retired_before_the_delete_id_was_recorded_still_explains_itself(local_db):
    with storage.connect() as conn:
        a, b = _chain(conn, 2)
        storage.delete_memory(conn, b)
        conn.execute("UPDATE tombstone_components SET reason = 'deleted' WHERE memory_id = ?", (a,))
        conn.commit()
    got = asyncio.run(server.memory_get(a))
    assert got["retired"] is True and "was not recorded" in got["message"]


def test_delete_memories_names_each_deleted_id(local_db):
    with storage.connect() as conn:
        a, b = _chain(conn, 2)
        assert storage.delete_memories(conn, [b]) == 1
        assert storage.retirement_note(conn, a)["deleted_id"] == b
