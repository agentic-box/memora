"""Issues and TODOs are created ONLY by an explicit caller — never inferred.

History: a keyword classifier used to stamp type=issue/status=open onto any memory
whose text contained enough bug vocabulary. It mislabelled 130 knowledge memories in
the live store. Two mechanical defects were fixed first (substring bleed, where
"fault" matched inside DEFAULT and "patch" inside DISPATCH; and double counting,
where "resolve"/"resolved" were separate entries so one word scored 2), but that
still left the real limitation: word frequency cannot distinguish a note ABOUT a bug
from a bug REPORT. A retrospective describing a fix scored 5 legitimate whole-word
hits and was filed as an open issue.

So auto-detection was removed entirely. These tests pin that decision: content that
is *about* bugs must stay untyped knowledge when written through the ordinary paths.
"""
import pytest

import memora.storage as storage


BUG_HEAVY = (
    "The bug was a regression: a crash in the error path. The fix resolved the "
    "defect, and the patch is a hotfix for the broken issue."
)

ISSUE_SHAPED_REPORT = (
    "**clmux: workspace rename does not work**\n\n"
    "Attempting to rename a workspace does not take effect. The new name fails to "
    "apply, reverts on next render, or is not persisted across a daemon restart.\n"
    "**Fix ideas:** confirm the rename mutation calls markDirty before saving."
)


def test_auto_detection_helpers_are_gone():
    """The classifier must not come back by accident.

    If someone reintroduces it, every other test here would silently start
    exercising it again, so assert the absence directly.
    """
    for name in (
        "_detect_memory_type",
        "_apply_auto_detection",
        "_ISSUE_KEYWORDS",
        "_ISSUE_KEYWORD_RES",
        "_RESOLVED_PATTERNS",
    ):
        assert not hasattr(storage, name), f"{name} was reintroduced"


@pytest.mark.parametrize(
    "content",
    [BUG_HEAVY, ISSUE_SHAPED_REPORT],
    ids=["bug-vocabulary", "issue-shaped-report"],
)
def test_plain_create_never_infers_an_issue(local_db, content):
    with storage.connect() as conn:
        created = storage.add_memory(conn, content=content, tags=["test"])
        got = storage.get_memory(conn, created["id"])

    meta = got.get("metadata") or {}
    assert "type" not in meta, f"unexpectedly typed: {meta.get('type')}"
    assert "status" not in meta
    assert "severity" not in meta
    assert "memora/issues" not in (got.get("tags") or [])


def test_batch_create_never_infers_an_issue(local_db):
    with storage.connect() as conn:
        created = storage.add_memories(
            conn,
            [{"content": BUG_HEAVY, "tags": ["test"]},
             {"content": ISSUE_SHAPED_REPORT, "tags": ["test"]}],
        )
        rows = [storage.get_memory(conn, c["id"]) for c in created]

    for row in rows:
        meta = row.get("metadata") or {}
        assert "type" not in meta
        assert "memora/issues" not in (row.get("tags") or [])


def test_explicit_issue_metadata_is_preserved(local_db):
    """memory_create_issue works by passing type explicitly — that must still land."""
    with storage.connect() as conn:
        created = storage.add_memory(
            conn,
            content="A genuine filed report",
            tags=["memora/issues"],
            metadata={"type": "issue", "status": "open", "severity": "major"},
        )
        got = storage.get_memory(conn, created["id"])

    meta = got["metadata"]
    assert meta["type"] == "issue"
    assert meta["status"] == "open"
    assert meta["severity"] == "major"
    assert "memora/issues" in got["tags"]


def test_explicit_todo_metadata_is_preserved(local_db):
    with storage.connect() as conn:
        created = storage.add_memory(
            conn,
            content="Remember to implement the thing",
            tags=["memora/todos"],
            metadata={"type": "todo", "status": "open"},
        )
        got = storage.get_memory(conn, created["id"])

    assert got["metadata"]["type"] == "todo"
    assert "memora/todos" in got["tags"]
