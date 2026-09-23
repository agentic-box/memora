"""scripts/apply_backfill_47.py: applies exactly the approved, still-current rows."""

import hashlib
import json

import pytest

import memora
import memora.storage as storage
from scripts import apply_backfill_47 as apply

PROJECTS = ["memora", "clmux", "acebar", "pi"]
ISSUE = "**clmux: workspace rename does not work**  Attempting to rename a workspace does not take."
NOTE = "Observability matters because clmux daemon logs are what the sidebar replays."


@pytest.fixture(params=["local_db", "fake_d1_backend"])
def store(request, monkeypatch):
    request.getfixturevalue(request.param)
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps(PROJECTS))
    monkeypatch.setattr(memora, "TAG_WHITELIST", set(memora.DEFAULT_TAGS))  # the real default policy
    return request.param


def _row(conn, content, tags, metadata):
    """Insert as the legacy write path left it (bypassing today's rules)."""
    cur = conn.execute("INSERT INTO memories (content, metadata, tags) VALUES (?, ?, ?)",
                       (content, json.dumps(metadata), json.dumps(tags)))
    mid = cur.lastrowid
    storage._upsert_embedding(conn, mid, storage._compute_embedding(content, metadata, tags))
    return mid


def _preview_row(mid, content, tags, metadata, target, retag, *, approved=True, status="proposed", sha=True):
    stored = {"section": metadata.get("section"), "subsection": metadata.get("subsection"),
              "tags": list(tags), "metadata_project": metadata.get("project"), "type": metadata.get("type")}
    if sha:
        stored["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    row = {"id": mid, "preview": content[:120], "stored": stored, "approved": approved, "status": status}
    if status == "proposed":
        row["proposal"] = {"set_metadata_project": target, "retag_typed": retag, "alternative_marker_tag": None}
    return row


def _seed(conn):
    ids, contradictions, keyword_only = {}, [], []
    issue_meta = {"type": "issue", "status": "open", "section": "clmux"}
    ids["contradiction"] = _row(conn, ISSUE, ["memora/issues"], issue_meta)
    contradictions.append(_preview_row(ids["contradiction"], ISSUE, ["memora/issues"], issue_meta,
                                       "clmux", {"memora/issues": "clmux/issues"}, sha=False))
    note_meta = {"section": "clmux", "subsection": "architecture"}
    ids["keyword"] = _row(conn, NOTE, ["architecture"], note_meta)
    keyword_only.append(_preview_row(ids["keyword"], NOTE, ["architecture"], note_meta, "clmux", {}))
    ids["needs_human"] = _row(conn, "A neutral note.", ["memora/todos"], {"type": "todo", "section": "memora"})
    keyword_only.append(_preview_row(ids["needs_human"], "A neutral note.", ["memora/todos"],
                                     {"type": "todo", "section": "memora"}, None, {},
                                     approved=False, status="needs-human"))
    stale_meta = {"type": "issue", "section": "clmux"}
    ids["stale"] = _row(conn, ISSUE + " stale", ["memora/issues"], stale_meta)
    contradictions.append(_preview_row(ids["stale"], ISSUE + " stale", ["memora/issues"], stale_meta,
                                       "clmux", {"memora/issues": "clmux/issues"}))
    conn.execute("UPDATE memories SET tags = ? WHERE id = ?",
                 (json.dumps(["memora/issues", "clmux/tui"]), ids["stale"]))  # edited since the preview
    pend_meta = {"type": "issue", "section": "clmux", "import_attempt": "0" * 32 + ":1:0"}
    ids["pending"] = _row(conn, ISSUE + " pending", ["memora/issues"], pend_meta)
    contradictions.append(_preview_row(ids["pending"], ISSUE + " pending", ["memora/issues"], pend_meta,
                                       "clmux", {"memora/issues": "clmux/issues"}))
    done_meta = {"type": "issue", "section": "clmux", "project": "clmux"}
    ids["already"] = _row(conn, ISSUE + " done", ["clmux/issues"], done_meta)
    contradictions.append(_preview_row(ids["already"], ISSUE + " done", ["memora/issues"],
                                       {"type": "issue", "section": "clmux"},
                                       "clmux", {"memora/issues": "clmux/issues"}))
    ret_meta = {"section": "clmux"}
    ids["retired"] = _row(conn, NOTE + " retired", [], ret_meta)
    conn.execute("INSERT INTO tombstones (content_hash, memory_id, reason) VALUES (?, ?, 'test')",
                 ("h", ids["retired"]))
    keyword_only.append(_preview_row(ids["retired"], NOTE + " retired", [], ret_meta, "clmux", {}))
    bad_meta = {"section": "trader"}
    ids["bad_target"] = _row(conn, NOTE + " bad", [], bad_meta)
    keyword_only.append(_preview_row(ids["bad_target"], NOTE + " bad", [], bad_meta, "trader", {}))
    conn.commit()
    preview = {"summary": {}, "contradictions": contradictions, "keyword_only": keyword_only,
               "approval": {"approved_by": "test", "at": "2026-09-23", "rule": "proposed rows"}}
    return ids, preview


def _state(conn, mid):
    row = conn.execute("SELECT content, metadata, tags FROM memories WHERE id = ?", (mid,)).fetchone()
    return row[0], json.loads(row[1]) if row[1] else {}, json.loads(row[2])


def _write_preview(tmp_path, preview):
    p = tmp_path / "approved.json"
    p.write_text(json.dumps(preview))
    return p


def test_applies_exactly_the_approved_current_rows(store, tmp_path, capsys):
    with storage.connect() as conn:
        ids, preview = _seed(conn)
        before = {k: _state(conn, v) for k, v in ids.items()}
        vectors = {k: storage._get_embeddings_for_ids(conn, [v]).get(v) for k, v in ids.items()}
    report_path = tmp_path / "report.json"
    rc = apply.main(["--preview", str(_write_preview(tmp_path, preview)), "--report", str(report_path)])
    assert rc == 0
    report = json.loads(report_path.read_text())
    outcomes = {r["id"]: r["outcome"] for r in report["rows"]}
    assert outcomes == {
        ids["contradiction"]: "applied",
        ids["keyword"]: "applied",
        ids["stale"]: "skipped-stale",
        ids["pending"]: "skipped-pending",
        ids["already"]: "already-applied",
        ids["retired"]: "skipped-retired",
        ids["bad_target"]: "refused",
    }  # the unapproved needs-human row is not even considered
    assert ids["needs_human"] not in outcomes
    with storage.connect() as conn:
        after = {k: _state(conn, v) for k, v in ids.items()}
        new_vectors = {k: storage._get_embeddings_for_ids(conn, [v]).get(v) for k, v in ids.items()}
    # The two applied rows: exactly metadata.project added and the typed tag re-prefixed.
    content, meta, tags = after["contradiction"]
    assert content == before["contradiction"][0]
    # metadata.project added; the normal update path also stores memora's
    # canonical form (hierarchy.path derived from section), as any edit does.
    assert meta == storage._prepare_metadata({**before["contradiction"][1], "project": "clmux"})
    assert {k: v for k, v in meta.items() if k != "hierarchy"} == {**before["contradiction"][1], "project": "clmux"}
    assert tags == ["clmux/issues"]
    content, meta, tags = after["keyword"]
    assert content == before["keyword"][0] and tags == ["architecture"]
    assert meta == storage._prepare_metadata({**before["keyword"][1], "project": "clmux"})
    assert meta["section"] == "clmux" and meta["subsection"] == "architecture"  # unchanged
    # The embedding was refreshed for the edited rows (metadata is part of the embedding text).
    assert new_vectors["contradiction"] is not None
    # Every other row is byte-for-byte untouched.
    for key in ("needs_human", "stale", "pending", "already", "retired", "bad_target"):
        assert after[key] == before[key], key
        assert new_vectors[key] == vectors[key], key
    assert "stale" in [r for r in report["rows"] if r["id"] == ids["stale"]][0]["outcome"]
    stale = next(r for r in report["rows"] if r["id"] == ids["stale"])
    assert "tags" in stale["detail"]

    # Idempotent: a second run applies nothing new.
    report2 = tmp_path / "report2.json"
    assert apply.main(["--preview", str(_write_preview(tmp_path, preview)), "--report", str(report2)]) == 0
    outcomes2 = {r["id"]: r["outcome"] for r in json.loads(report2.read_text())["rows"]}
    assert outcomes2[ids["contradiction"]] == outcomes2[ids["keyword"]] == "already-applied"
    with storage.connect() as conn:
        assert {k: _state(conn, v) for k, v in ids.items()} == after


def test_a_dry_run_writes_nothing(store, tmp_path, capsys):
    with storage.connect() as conn:
        ids, preview = _seed(conn)
        before = {k: _state(conn, v) for k, v in ids.items()}
    report_path = tmp_path / "dry.json"
    assert apply.main(["--preview", str(_write_preview(tmp_path, preview)), "--dry-run",
                       "--report", str(report_path)]) == 0
    rows = {r["id"]: r for r in json.loads(report_path.read_text())["rows"]}
    assert rows[ids["contradiction"]]["outcome"] == "would-apply"
    assert rows[ids["contradiction"]]["after"]["tags"] == ["clmux/issues"]
    assert "DRY RUN (nothing written)" in capsys.readouterr().out
    with storage.connect() as conn:
        assert {k: _state(conn, v) for k, v in ids.items()} == before


def test_a_content_change_since_the_preview_is_stale(store, tmp_path):
    with storage.connect() as conn:
        ids, preview = _seed(conn)
        # Past the 120-char preview: only the recorded content_sha256 can see it.
        conn.execute("UPDATE memories SET content = content || ' edited later' WHERE id = ?", (ids["keyword"],))
        conn.commit()
    report_path = tmp_path / "r.json"
    apply.main(["--preview", str(_write_preview(tmp_path, preview)), "--report", str(report_path)])
    row = next(r for r in json.loads(report_path.read_text())["rows"] if r["id"] == ids["keyword"])
    assert row["outcome"] == "skipped-stale" and "content_sha256" in row["detail"]


def test_a_preview_without_an_approval_block_is_refused(store, tmp_path):
    with storage.connect() as conn:
        _ids, preview = _seed(conn)
    preview.pop("approval")
    with pytest.raises(SystemExit, match="no approval block"):
        apply.main(["--preview", str(_write_preview(tmp_path, preview))])


def test_a_superseded_row_is_skipped(store, tmp_path):
    with storage.connect() as conn:
        ids, preview = _seed(conn)
        newer = storage.add_memory(conn, content="newer version of the note", tags=[])
        storage.add_link(conn, newer["id"], ids["keyword"], edge_type="supersedes")
        conn.commit()
    report_path = tmp_path / "r.json"
    apply.main(["--preview", str(_write_preview(tmp_path, preview)), "--report", str(report_path)])
    row = next(r for r in json.loads(report_path.read_text())["rows"] if r["id"] == ids["keyword"])
    assert row["outcome"] == "skipped-retired" and row["detail"] == "superseded"
