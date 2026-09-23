"""scripts/apply_backfill_47.py: applies exactly the approved, still-current
rows of an approved preview -- guarded, fail-stop, repairable, verified."""

import hashlib
import json

import pytest

import memora
import memora.storage as storage
from scripts import apply_backfill_47 as apply
from scripts import preview_backfill_47 as preview_mod

PROJECTS = ["memora", "clmux", "acebar", "pi"]
ISSUE = "**clmux: workspace rename does not work**  Attempting to rename a workspace does not take."
NOTE = "Observability matters because clmux daemon logs are what the sidebar replays."


@pytest.fixture(params=["local_db", "fake_d1_backend"])
def store(request, monkeypatch):
    request.getfixturevalue(request.param)
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps(PROJECTS))
    monkeypatch.setattr(memora, "TAG_WHITELIST", set(memora.DEFAULT_TAGS))  # the real default policy
    return request.param


@pytest.fixture
def d1(fake_d1_backend, monkeypatch):
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps(PROJECTS))
    monkeypatch.setattr(memora, "TAG_WHITELIST", set(memora.DEFAULT_TAGS))
    return fake_d1_backend


@pytest.fixture
def sqlite_store(local_db, monkeypatch):
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps(PROJECTS))
    monkeypatch.setattr(memora, "TAG_WHITELIST", set(memora.DEFAULT_TAGS))


def _row(conn, content, tags, metadata):
    """Insert as the legacy write path left it (bypassing today's rules)."""
    cur = conn.execute("INSERT INTO memories (content, metadata, tags) VALUES (?, ?, ?)",
                       (content, json.dumps(metadata), json.dumps(tags)))
    mid = cur.lastrowid
    storage._upsert_embedding(conn, mid, storage._compute_embedding(content, metadata, tags))
    storage._fts_upsert(conn, mid, content, json.dumps(metadata), json.dumps(tags))
    return mid


def _preview_row(mid, content, tags, metadata, target, *, approved=True, status="proposed"):
    stored = {"section": metadata.get("section"), "subsection": metadata.get("subsection"),
              "tags": list(tags), "metadata_project": metadata.get("project"), "type": metadata.get("type"),
              "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
              "metadata_sha256": preview_mod.metadata_sha256(json.dumps(metadata)),
              "metadata": dict(metadata)}
    row = {"id": mid, "preview": content[:120], "stored": stored, "approved": approved, "status": status}
    if status == "proposed":
        retag = {t: storage.project_tag(target, storage._typed_tag_kind(t))
                 for t in storage._existing_system_tags(tags, {"type": metadata.get("type")})
                 if t != storage.project_tag(target, storage._typed_tag_kind(t))}
        row["proposal"] = {"set_metadata_project": target, "retag_typed": retag, "alternative_marker_tag": None}
    return row


APPROVAL = {"approved_by": "test", "at": "2026-09-23", "rule": "proposed rows"}


def _preview(contradictions, keyword_only):
    return {"summary": {}, "contradictions": contradictions, "keyword_only": keyword_only,
            "approval": dict(APPROVAL)}


def _seed(conn, *, with_skips=True):
    ids, contradictions, keyword_only = {}, [], []
    issue_meta = {"type": "issue", "status": "open", "section": "clmux"}
    ids["contradiction"] = _row(conn, ISSUE, ["memora/issues"], issue_meta)
    contradictions.append(_preview_row(ids["contradiction"], ISSUE, ["memora/issues"], issue_meta, "clmux"))
    note_meta = {"section": "clmux", "subsection": "architecture"}
    ids["keyword"] = _row(conn, NOTE, ["architecture"], note_meta)
    keyword_only.append(_preview_row(ids["keyword"], NOTE, ["architecture"], note_meta, "clmux"))
    ids["needs_human"] = _row(conn, "A neutral note.", ["memora/todos"], {"type": "todo", "section": "memora"})
    keyword_only.append(_preview_row(ids["needs_human"], "A neutral note.", ["memora/todos"],
                                     {"type": "todo", "section": "memora"}, None,
                                     approved=False, status="needs-human"))
    if with_skips:
        stale_meta = {"type": "issue", "section": "clmux"}
        ids["stale"] = _row(conn, ISSUE + " stale", ["memora/issues"], stale_meta)
        contradictions.append(_preview_row(ids["stale"], ISSUE + " stale", ["memora/issues"], stale_meta, "clmux"))
        conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                     (json.dumps(["memora/issues", "clmux/tui"]), ids["stale"]))  # edited since the preview
        pend_meta = {"type": "issue", "section": "clmux", "import_attempt": "0" * 32 + ":1:0"}
        ids["pending"] = _row(conn, ISSUE + " pending", ["memora/issues"], pend_meta)
        contradictions.append(_preview_row(ids["pending"], ISSUE + " pending", ["memora/issues"], pend_meta,
                                           "clmux"))
        ret_meta = {"section": "clmux"}
        ids["retired"] = _row(conn, NOTE + " retired", [], ret_meta)
        conn.execute("INSERT INTO tombstones (content_hash, memory_id, reason) VALUES (?, ?, 'test')",
                     ("h", ids["retired"]))
        keyword_only.append(_preview_row(ids["retired"], NOTE + " retired", [], ret_meta, "clmux"))
    conn.commit()
    return ids, _preview(contradictions, keyword_only)


def _state(conn, mid):
    row = conn.execute("SELECT content, metadata, tags FROM memories WHERE id = ?", (mid,)).fetchone()
    return row[0], json.loads(row[1]) if row[1] else {}, json.loads(row[2])


def _file(tmp_path, preview, name="approved.json"):
    p = tmp_path / name
    p.write_text(json.dumps(preview))
    return p


def _run(tmp_path, preview, *extra, name="r.json"):
    report = tmp_path / name
    rc = apply.main(["--preview", str(_file(tmp_path, preview)), "--report", str(report), *extra])
    data = json.loads(report.read_text())
    return rc, {r["id"]: r for r in data["rows"]}


# --- the happy path, exactness and idempotency --------------------------------------

def test_applies_exactly_the_approved_current_rows(store, tmp_path):
    with storage.connect() as conn:
        ids, preview = _seed(conn)
        before = {k: _state(conn, v) for k, v in ids.items()}
        vectors = {k: storage._get_embeddings_for_ids(conn, [v]).get(v) for k, v in ids.items()}
    rc, rows = _run(tmp_path, preview, "--allow-skips")
    assert rc == 0
    assert {mid: r["outcome"] for mid, r in rows.items()} == {
        ids["contradiction"]: "applied", ids["keyword"]: "applied", ids["stale"]: "skipped-stale",
        ids["pending"]: "skipped-pending", ids["retired"]: "skipped-retired",
    }
    assert ids["needs_human"] not in rows  # unapproved: not even considered
    with storage.connect() as conn:
        after = {k: _state(conn, v) for k, v in ids.items()}
        new_vectors = {k: storage._get_embeddings_for_ids(conn, [v]).get(v) for k, v in ids.items()}
    content, meta, tags = after["contradiction"]
    assert content == before["contradiction"][0] and tags == ["clmux/issues"]
    assert meta == {**before["contradiction"][1], "project": "clmux", "hierarchy": {"path": ["clmux"]}}
    content, meta, tags = after["keyword"]
    assert content == before["keyword"][0] and tags == ["architecture"]
    assert meta == {**before["keyword"][1], "project": "clmux", "hierarchy": {"path": ["clmux", "architecture"]}}
    assert new_vectors["contradiction"] != vectors["contradiction"]  # re-embedded with the new metadata
    for key in ("needs_human", "stale", "pending", "retired"):
        assert after[key] == before[key] and new_vectors[key] == vectors[key], key
    # Without --allow-skips the same outcomes fail the exit status; a re-run is idempotent.
    rc2, rows2 = _run(tmp_path, preview, name="r2.json")
    assert rc2 == 1
    assert rows2[ids["contradiction"]]["outcome"] == rows2[ids["keyword"]]["outcome"] == "already-applied"
    with storage.connect() as conn:
        assert {k: _state(conn, v) for k, v in ids.items()} == after


def test_a_clean_run_exits_zero_without_allow_skips(store, tmp_path):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    rc, rows = _run(tmp_path, preview)
    assert rc == 0 and {r["outcome"] for r in rows.values()} == {"applied"}
    rc, rows = _run(tmp_path, preview, name="again.json")
    assert rc == 0 and {r["outcome"] for r in rows.values()} == {"already-applied"}


def test_a_refused_row_fails_the_exit_status_even_with_allow_skips(store, tmp_path):
    with storage.connect() as conn:
        mid = _row(conn, NOTE + " bad", [], {"section": "trader"})
        conn.commit()
    preview = _preview([], [_preview_row(mid, NOTE + " bad", [], {"section": "trader"}, "trader")])
    rc, rows = _run(tmp_path, preview, "--allow-skips")
    assert rc == 1 and rows[mid]["outcome"] == "refused"


def test_a_dry_run_writes_nothing_and_prints_the_expected_diff(store, tmp_path, capsys):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        before = {k: _state(conn, v) for k, v in ids.items()}
    rc, rows = _run(tmp_path, preview, "--dry-run")
    assert rc == 0
    row = rows[ids["contradiction"]]
    assert row["outcome"] == "would-apply"
    assert row["diff"]["tags"] == [["memora/issues"], ["clmux/issues"]]
    assert row["diff"]["project"] == [None, "clmux"]
    out = capsys.readouterr().out
    assert "DRY RUN (nothing written)" in out and '"clmux/issues"' in out
    with storage.connect() as conn:
        assert {k: _state(conn, v) for k, v in ids.items()} == before


# --- 6. the artifact is validated before any connection --------------------------------

@pytest.mark.parametrize("tamper", ["approved_by", "duplicate", "expect_count", "retag", "no_metadata_hash"])
def test_a_bad_artifact_is_refused_before_connecting(store, tmp_path, monkeypatch, tamper):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    extra = []
    if tamper == "approved_by":
        preview["approval"]["approved_by"] = " "
    elif tamper == "duplicate":
        preview["keyword_only"].append(dict(preview["contradictions"][0]))
    elif tamper == "expect_count":
        extra = ["--expect-count", "5"]
    elif tamper == "retag":
        preview["contradictions"][0]["proposal"]["retag_typed"] = {"memora/issues": "pi/issues"}
    else:
        del preview["keyword_only"][0]["stored"]["metadata_sha256"]
    for name in ("connect", "connect_without_schema"):
        monkeypatch.setattr(storage, name, lambda *a, **k: pytest.fail("connected before validating"))
    with pytest.raises(SystemExit, match="refused"):
        apply.main(["--preview", str(_file(tmp_path, preview)), *extra])


def test_the_expected_count_is_accepted_when_it_matches(store, tmp_path):
    with storage.connect() as conn:
        _ids, preview = _seed(conn, with_skips=False)
    rc, _rows = _run(tmp_path, preview, "--expect-count", "2")
    assert rc == 0


# --- 1. full-content and full-metadata fingerprints ----------------------------------------

@pytest.mark.parametrize("change", ["content", "metadata"])
def test_a_change_the_old_120_char_preview_could_not_see_is_stale(store, tmp_path, change):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        if change == "content":
            conn.execute("UPDATE memories SET content = content || ' edited later' WHERE id = ?", (ids["keyword"],))
        else:
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?",
                         (json.dumps({"section": "clmux", "subsection": "architecture", "note": "x"}),
                          ids["keyword"]))
        conn.commit()
    rc, rows = _run(tmp_path, preview)
    assert rc == 1 and rows[ids["keyword"]]["outcome"] == "skipped-stale"
    assert f"{change}_sha256" in rows[ids["keyword"]]["detail"]


# --- 4. write scope ----------------------------------------------------------------------------

@pytest.mark.parametrize("legacy_meta", [
    {"section": "clmux", "hierarchy": ["clmux", "old"]},          # a legacy hierarchy form it would rewrite
    {"section": "clmux", "images": [{"src": "data:image/png;base64,AAAA"}]},  # images it would process
])
def test_a_wider_canonicalisation_is_refused_and_never_written(store, tmp_path, legacy_meta):
    with storage.connect() as conn:
        mid = _row(conn, NOTE + " legacy", [], legacy_meta)
        conn.commit()
        before = _state(conn, mid)
    preview = _preview([], [_preview_row(mid, NOTE + " legacy", [], legacy_meta, "clmux")])
    for flag in (["--dry-run"], []):
        rc, rows = _run(tmp_path, preview, *flag)
        assert rc == 1 and rows[mid]["outcome"] == "canonicalization-diff"
    with storage.connect() as conn:
        assert _state(conn, mid) == before


# --- 2. the guarded write: a concurrent change is never overwritten -------------------------

def test_a_concurrent_update_between_check_and_write_is_raced_and_stops(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    real = storage._compute_embedding
    state = {"done": False}

    def concurrent_write_then_embed(content, metadata, tags):
        if not state["done"]:
            state["done"] = True  # another writer lands after the checks, before the UPDATE
            with d1.connect() as other:
                other.execute("UPDATE memories SET content = content || ' concurrent' WHERE id = ?",
                              (ids["contradiction"],))
        return real(content, metadata, tags)

    monkeypatch.setattr(storage, "_compute_embedding", concurrent_write_then_embed)
    rc, rows = _run(tmp_path, preview)
    monkeypatch.setattr(storage, "_compute_embedding", real)
    assert rc == 1
    assert rows[ids["contradiction"]]["outcome"] == "raced"
    assert rows[ids["keyword"]]["outcome"] == "not-attempted"
    with storage.connect() as conn:
        content, meta, tags = _state(conn, ids["contradiction"])
    assert content.endswith(" concurrent") and "project" not in meta and tags == ["memora/issues"]


def test_the_update_guard_itself(store):
    with storage.connect() as conn:
        mid = _row(conn, NOTE, ["architecture"], {"section": "clmux"})
        conn.commit()
        row = conn.execute("SELECT content, metadata, tags, updated_at FROM memories WHERE id = ?", (mid,)).fetchone()
        guard = {"content": row[0], "metadata": row[1], "tags": row[2], "updated_at": row[3], "related": None}
        with pytest.raises(storage.ConcurrentUpdateError):
            storage.update_memory(conn, mid, metadata={"project": "clmux"}, expected_row=dict(guard, tags="[]"))
        assert "project" not in _state(conn, mid)[1]
        storage.update_memory(conn, mid, metadata={"project": "clmux"}, expected_row=guard)
        assert _state(conn, mid)[1]["project"] == "clmux"


# --- 3. fail-stop, and a re-run repairs a part-written D1 row --------------------------------

def test_a_d1_write_that_stops_part_way_is_repaired_by_a_rerun(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    real = storage._upsert_embedding
    state = {"failed": False}

    def fail_first(conn, mid, vec):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("injected embedding write failure")
        return real(conn, mid, vec)

    monkeypatch.setattr(storage, "_upsert_embedding", fail_first)
    rc, rows = _run(tmp_path, preview)
    monkeypatch.setattr(storage, "_upsert_embedding", real)
    assert rc == 1
    first = rows[ids["contradiction"]]
    assert first["outcome"] == "failed" and "injected embedding write failure" in first["detail"]
    assert rows[ids["keyword"]]["outcome"] == "not-attempted"
    with storage.connect() as conn:
        _content, meta, tags = _state(conn, ids["contradiction"])
    assert meta["project"] == "clmux" and tags == ["clmux/issues"]  # the row itself was written (D1)
    rc, rows = _run(tmp_path, preview, name="rerun.json")
    assert rc == 0
    assert rows[ids["contradiction"]]["outcome"] == "repaired"
    assert "embedding stale" in rows[ids["contradiction"]]["detail"]
    assert rows[ids["keyword"]]["outcome"] == "applied"
    rc, rows = _run(tmp_path, preview, name="third.json")
    assert rc == 0 and {r["outcome"] for r in rows.values()} == {"already-applied"}


def test_a_sqlite_write_that_fails_part_way_rolls_back(sqlite_store, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        before = _state(conn, ids["contradiction"])
    real = storage._fts_upsert

    def boom(*a, **k):
        raise RuntimeError("injected FTS failure")

    monkeypatch.setattr(storage, "_fts_upsert", boom)
    rc, rows = _run(tmp_path, preview)
    monkeypatch.setattr(storage, "_fts_upsert", real)
    assert rc == 1 and rows[ids["contradiction"]]["outcome"] == "failed"
    assert "injected FTS failure" in rows[ids["contradiction"]]["detail"]
    with storage.connect() as conn:
        assert _state(conn, ids["contradiction"]) == before  # one transaction: rolled back
    rc, rows = _run(tmp_path, preview, name="rerun.json")
    assert rc == 0 and rows[ids["contradiction"]]["outcome"] == "applied"


def test_a_lost_lease_stops_the_run(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    real = storage.update_memory

    def update_then_lose_lease(conn, *a, **k):
        out = real(conn, *a, **k)
        conn.execute("UPDATE import_lease SET owner = 'intruder'")
        return out

    monkeypatch.setattr(storage, "update_memory", update_then_lose_lease)
    rc, rows = _run(tmp_path, preview)
    assert rc == 1
    assert rows[ids["contradiction"]]["outcome"] == "applied"
    assert rows[ids["keyword"]]["outcome"] == "failed" and "lease lost" in rows[ids["keyword"]]["detail"]


def test_a_superseded_row_is_skipped(store, tmp_path):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        newer = storage.add_memory(conn, content="newer version of the note", tags=[])
        storage.add_link(conn, newer["id"], ids["keyword"], edge_type="supersedes")
        conn.commit()
    _rc, rows = _run(tmp_path, preview)
    assert rows[ids["keyword"]]["outcome"] == "skipped-retired"
    assert rows[ids["keyword"]]["detail"] == "superseded"


def test_a_preview_without_an_approval_block_is_refused(store, tmp_path):
    with storage.connect() as conn:
        _ids, preview = _seed(conn, with_skips=False)
    preview.pop("approval")
    with pytest.raises(SystemExit, match="no approval block"):
        apply.main(["--preview", str(_file(tmp_path, preview))])


def test_on_sqlite_the_checks_and_the_write_hold_the_write_lock(sqlite_store, tmp_path, monkeypatch):
    """BEGIN IMMEDIATE: between the re-read and the UPDATE no other writer can
    commit (it gets 'database is locked'), so the checks cannot go stale."""
    import sqlite3 as _sqlite3

    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    db_path = storage.STORAGE_BACKEND.db_path
    real = storage._compute_embedding
    seen = {}

    def other_writer_tries(content, metadata, tags):
        if "outcome" not in seen:
            other = _sqlite3.connect(db_path, timeout=0)
            try:
                other.execute("UPDATE memories SET content = content || ' x' WHERE id = ?", (ids["keyword"],))
                other.commit()
                seen["outcome"] = "wrote"
            except _sqlite3.OperationalError as exc:
                seen["outcome"] = str(exc)
            finally:
                other.close()
        return real(content, metadata, tags)

    monkeypatch.setattr(storage, "_compute_embedding", other_writer_tries)
    rc, _rows = _run(tmp_path, preview)
    monkeypatch.setattr(storage, "_compute_embedding", real)
    assert rc == 0 and seen["outcome"] == "database is locked"


def test_the_post_write_read_back_catches_anything_outside_the_allowed_diff(store, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    real = storage.update_memory

    def update_and_touch_more(conn, memory_id, **kw):
        out = real(conn, memory_id, **kw)
        conn.execute("UPDATE memories SET importance = 7 WHERE id = ?", (memory_id,))
        conn.commit()
        return out

    monkeypatch.setattr(storage, "update_memory", update_and_touch_more)
    rc, rows = _run(tmp_path, preview)
    assert rc == 1
    first = rows[ids["contradiction"]]
    assert first["outcome"] == "failed" and "memories.importance changed" in first["detail"]
    assert rows[ids["keyword"]]["outcome"] == "not-attempted"


# --- round 3 (review 7469) -----------------------------------------------------------

def _raise_once(monkeypatch, target, name, exc):
    real = getattr(target, name)
    state = {"done": False}

    def wrapped(*a, **k):
        if not state["done"]:
            state["done"] = True
            raise exc
        return real(*a, **k)

    monkeypatch.setattr(target, name, wrapped)
    return real


def test_a_verification_error_on_d1_stops_and_the_rerun_reassesses(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    real = _raise_once(monkeypatch, apply, "verify_write", RuntimeError("injected read-back failure"))
    rc, rows = _run(tmp_path, preview)
    monkeypatch.setattr(apply, "verify_write", real)
    assert rc == 1
    first = rows[ids["contradiction"]]
    assert first["outcome"] == "verify-error" and "WRITTEN, not verified" in first["detail"]
    assert rows[ids["keyword"]]["outcome"] == "not-attempted"
    rc, rows = _run(tmp_path, preview, name="rerun.json")
    assert rc == 0
    assert rows[ids["contradiction"]]["outcome"] == "already-applied"  # reassessed, not re-applied blindly
    assert rows[ids["keyword"]]["outcome"] == "applied"


def test_a_verification_error_on_sqlite_rolls_the_row_back(sqlite_store, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        before = _state(conn, ids["contradiction"])
    real = _raise_once(monkeypatch, apply, "verify_write", RuntimeError("injected read-back failure"))
    rc, rows = _run(tmp_path, preview)
    monkeypatch.setattr(apply, "verify_write", real)
    assert rc == 1 and rows[ids["contradiction"]]["outcome"] == "verify-error"
    assert "rolled back" in rows[ids["contradiction"]]["detail"]
    with storage.connect() as conn:
        assert _state(conn, ids["contradiction"]) == before
    rc, rows = _run(tmp_path, preview, name="rerun.json")
    assert rc == 0 and rows[ids["contradiction"]]["outcome"] == "applied"


@pytest.mark.parametrize("change", ["content", "other_metadata"])
def test_a_row_at_the_target_that_changed_otherwise_is_stale_not_repaired(store, tmp_path, change):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    assert _run(tmp_path, preview)[0] == 0  # applied: now at the target project and tags
    with storage.connect() as conn:
        if change == "content":  # past the old 120-char preview
            conn.execute("UPDATE memories SET content = content || ' edited later' WHERE id = ?",
                         (ids["contradiction"],))
        else:
            _c, meta, _t = _state(conn, ids["contradiction"])
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?",
                         (json.dumps(dict(meta, status="closed")), ids["contradiction"]))
        conn.commit()
        before = _state(conn, ids["contradiction"])
    rc, rows = _run(tmp_path, preview, name="rerun.json")
    assert rc == 1 and rows[ids["contradiction"]]["outcome"] == "skipped-stale"
    with storage.connect() as conn:
        assert _state(conn, ids["contradiction"]) == before  # never force-reindexed


@pytest.mark.parametrize("lag", ["provenance", "action"])
def test_doubtful_derived_state_is_repaired(store, tmp_path, lag):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    assert _run(tmp_path, preview)[0] == 0
    with storage.connect() as conn:
        if lag == "provenance":
            conn.execute("UPDATE memories_embeddings SET representation = 'dense', dimension = 3 "
                         "WHERE memory_id = ?", (ids["keyword"],))
        else:  # a later action: "update" is no longer the newest
            conn.execute("INSERT INTO memories_actions (memory_id, action, summary) VALUES (?, 'link', 'x')",
                         (ids["keyword"],))
        conn.commit()
    rc, rows = _run(tmp_path, preview, name="rerun.json")
    assert rc == 0 and rows[ids["keyword"]]["outcome"] == "repaired"
    assert ("embedding provenance" if lag == "provenance" else "action row") in rows[ids["keyword"]]["detail"]
    rc, rows = _run(tmp_path, preview, name="third.json")
    assert rc == 0 and rows[ids["keyword"]]["outcome"] == "already-applied"


def test_a_forward_only_supersedes_edge_at_assessment_is_skipped(store, tmp_path):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        newer = storage.add_memory(conn, content="a newer note", tags=[])
        storage.add_link(conn, newer["id"], ids["keyword"], edge_type="supersedes", bidirectional=False)
        conn.commit()
    _rc, rows = _run(tmp_path, preview)
    assert rows[ids["keyword"]]["outcome"] == "skipped-retired"
    assert f"supersedes edge from [{newer['id']}]" in rows[ids["keyword"]]["detail"]


def test_a_forward_only_supersedes_edge_between_check_and_write_is_raced(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        newer = storage.add_memory(conn, content="a newer issue", tags=[])
        conn.commit()
    real = storage._compute_embedding
    state = {"done": False}

    def edge_then_embed(content, metadata, tags):
        if not state["done"]:
            state["done"] = True
            with d1.connect() as other:
                storage.add_link(other, newer["id"], ids["contradiction"], edge_type="supersedes",
                                 bidirectional=False)
        return real(content, metadata, tags)

    monkeypatch.setattr(storage, "_compute_embedding", edge_then_embed)
    rc, rows = _run(tmp_path, preview)
    monkeypatch.setattr(storage, "_compute_embedding", real)
    assert rc == 1 and rows[ids["contradiction"]]["outcome"] == "raced"
    with storage.connect() as conn:
        assert "project" not in _state(conn, ids["contradiction"])[1]


# --- round 4 (review 7477) -----------------------------------------------------------

@pytest.mark.parametrize("related", [
    '["supersedes"]', '[1, null, "x"]', '[[{"id": %(A)d, "edge_type": "supersedes"}]]', '"supersedes"',
    None, 'not json {supersedes',
])
def test_any_crossrefs_json_is_tolerated_by_the_scan_and_the_guard(store, related):
    with storage.connect() as conn:
        a = _row(conn, NOTE, ["architecture"], {"section": "clmux"})
        b = _row(conn, NOTE + " other", [], {"section": "clmux"})
        value = related % {"A": a} if related else None
        conn.execute("INSERT OR REPLACE INTO memories_crossrefs (memory_id, related) VALUES (?, ?)", (b, value))
        conn.commit()
        assert storage._superseding_edge_sources(conn, a) == []
        row = conn.execute("SELECT content, metadata, tags, updated_at FROM memories WHERE id = ?", (a,)).fetchone()
        guard = {"content": row[0], "metadata": row[1], "tags": row[2], "updated_at": row[3], "related": None}
        storage.update_memory(conn, a, metadata={"project": "clmux"}, expected_row=guard)
        assert _state(conn, a)[1]["project"] == "clmux"
        # Positive control: a real object edge IS found.
        conn.execute("INSERT OR REPLACE INTO memories_crossrefs (memory_id, related) VALUES (?, ?)",
                     (b, json.dumps([1, "x", {"id": a, "edge_type": "supersedes"}])))
        conn.commit()
        assert storage._superseding_edge_sources(conn, a) == [b]


def test_a_transient_assessment_error_after_an_applied_row_is_reported(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn)  # considered: contradiction, stale, pending, keyword, retired
    seen = {"n": 0}

    def fail_third_row_select(sql, params):
        if sql.startswith("SELECT * FROM memories WHERE id = ?"):
            seen["n"] += 1
            return seen["n"] == 3  # row 1: assess + read-back; row 2: its assessment fails
        return False

    real_connect = d1.connect

    def connect(**kw):
        conn = real_connect(**kw)
        conn.fail_when = fail_third_row_select
        return conn

    monkeypatch.setattr(d1, "connect", connect)
    rc, rows = _run(tmp_path, preview, "--allow-skips")
    assert rc == 1
    assert rows[ids["contradiction"]]["outcome"] == "applied"
    assert rows[ids["stale"]]["outcome"] == "failed" and "assessment raised" in rows[ids["stale"]]["detail"]
    assert {rows[i]["outcome"] for i in (ids["pending"], ids["keyword"], ids["retired"])} == {"not-attempted"}


def test_a_lease_that_cannot_be_acquired_still_writes_the_report(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    real_connect = d1.connect

    def connect(**kw):
        conn = real_connect(**kw)
        conn.fail_when = lambda sql, params: sql.startswith("INSERT OR IGNORE INTO import_lease")
        return conn

    monkeypatch.setattr(d1, "connect", connect)
    report = tmp_path / "r.json"
    rc = apply.main(["--preview", str(_file(tmp_path, preview)), "--report", str(report)])
    data = json.loads(report.read_text())
    assert rc == 1 and data["summary"]["error"]
    assert {r["outcome"] for r in data["rows"]} == {"not-attempted"} and len(data["rows"]) == 2


def test_an_unexpected_error_during_the_write_is_uncertain(store, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
    real = apply.write

    def write_then_boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("connection reset after the write")

    monkeypatch.setattr(apply, "write", write_then_boom)
    rc, rows = _run(tmp_path, preview)
    assert rc == 1 and rows[ids["contradiction"]]["outcome"] == "uncertain"
    assert rows[ids["keyword"]]["outcome"] == "not-attempted"


def test_a_refused_artifact_still_writes_the_report(store, tmp_path):
    with storage.connect() as conn:
        _ids, preview = _seed(conn, with_skips=False)
    preview["approval"]["rule"] = ""
    report = tmp_path / "r.json"
    with pytest.raises(SystemExit, match="refused"):
        apply.main(["--preview", str(_file(tmp_path, preview)), "--report", str(report)])
    data = json.loads(report.read_text())
    assert data["summary"]["refused"] is True and "approval.rule" in data["summary"]["error"]


# --- round 5 (review 7483) -----------------------------------------------------------

def test_an_unknown_store_still_writes_the_report(store, tmp_path, monkeypatch):
    with storage.connect() as conn:
        _ids, preview = _seed(conn, with_skips=False)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"local": str(tmp_path / "l.db")}))
    report = tmp_path / "r.json"
    with pytest.raises(SystemExit, match="unknown store"):
        apply.main(["--preview", str(_file(tmp_path, preview)), "--db", "nosuch", "--report", str(report)])
    data = json.loads(report.read_text())
    assert data["summary"]["refused"] is True and "unknown store" in data["summary"]["error"]


def test_a_missing_report_directory_is_refused_before_connecting(store, tmp_path, monkeypatch):
    with storage.connect() as conn:
        _ids, preview = _seed(conn, with_skips=False)
    for name in ("connect", "connect_without_schema"):
        monkeypatch.setattr(storage, name, lambda *a, **k: pytest.fail("connected before the report preflight"))
    with pytest.raises(SystemExit, match="does not exist"):
        apply.main(["--preview", str(_file(tmp_path, preview)), "--report", str(tmp_path / "no" / "r.json")])


def test_a_report_write_failure_prints_the_full_report(store, tmp_path, monkeypatch, capsys):
    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)

    def unwritable(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(apply, "_write_report", unwritable)  # after the preflight passed
    rc = apply.main(["--preview", str(_file(tmp_path, preview)), "--report", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    assert rc == 1 and "REPORT WRITE FAILED" in out
    after = out.split("the full report follows:\n", 1)[1]
    fallback, _end = json.JSONDecoder().raw_decode(after)
    assert {r["id"]: r["outcome"] for r in fallback["rows"]} == {
        ids["contradiction"]: "applied", ids["keyword"]: "applied"}


def test_a_lease_whose_read_back_fails_is_still_released(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        _ids, preview = _seed(conn, with_skips=False)
    real_connect = d1.connect
    state = {"n": 0}

    def fail_first_read_back(sql, params):
        if sql.startswith("SELECT owner, lease_until FROM import_lease"):
            state["n"] += 1
            return state["n"] == 1  # the INSERT committed; its read-back fails
        return False

    def connect(**kw):
        conn = real_connect(**kw)
        conn.fail_when = fail_first_read_back
        return conn

    monkeypatch.setattr(d1, "connect", connect)
    rc, rows = _run(tmp_path, preview)
    monkeypatch.setattr(d1, "connect", real_connect)
    assert rc == 1 and set(r["outcome"] for r in rows.values()) == {"not-attempted"}
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_lease").fetchone()[0] == 0  # released


def test_an_interrupt_during_a_write_is_recorded_and_re_raised(d1, tmp_path, monkeypatch):
    with storage.connect() as conn:
        ids, preview = _seed(conn)  # considered: contradiction, stale, pending, keyword, retired
        # make row 4 (keyword) the second WRITE: rows 2-3 are skips
    real = storage.update_memory
    calls = {"n": 0}

    def interrupt_second(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt()
        return real(*a, **k)

    monkeypatch.setattr(storage, "update_memory", interrupt_second)
    report = tmp_path / "r.json"
    with pytest.raises(KeyboardInterrupt):
        apply.main(["--preview", str(_file(tmp_path, preview)), "--report", str(report), "--allow-skips"])
    data = json.loads(report.read_text())
    rows = {r["id"]: r["outcome"] for r in data["rows"]}
    assert rows[ids["contradiction"]] == "applied"
    assert rows[ids["keyword"]] == "uncertain"
    assert rows[ids["retired"]] == "not-attempted"
    assert data["summary"]["error"] == "interrupted: KeyboardInterrupt"
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_lease").fetchone()[0] == 0  # released in finally


# --- a live local primary is memora-all's alone (docs/local-primary-implementation.md §1 M10) ---

def test_a_live_primary_whose_lock_is_held_is_refused(sqlite_store, tmp_path, monkeypatch):
    import os

    from memora import backends

    with storage.connect() as conn:
        ids, preview = _seed(conn, with_skips=False)
        before = {k: _state(conn, v) for k, v in ids.items()}
    storage.STORAGE_BACKEND.store_name = "primary"
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"primary": "d1://acct/db"}))
    held = backends.acquire_primary_lock(storage.STORAGE_BACKEND.db_path)  # memora-all's hold
    try:
        with pytest.raises(SystemExit, match="primary-lock is held"):
            apply.main(["--preview", str(_file(tmp_path, preview))])
        with storage.connect() as conn:
            assert {k: _state(conn, v) for k, v in ids.items()} == before
    finally:
        os.close(held)
    rc, rows = _run(tmp_path, preview)  # no other holder: the script takes the lock itself
    assert rc == 0 and {r["outcome"] for r in rows.values()} == {"applied"}
