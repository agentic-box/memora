"""Item 258: memora-tidy-eval (memora/tidy_eval.py): the retrieval check, read-only, deterministic."""
import contextlib
import hashlib
import json
from pathlib import Path

import pytest

import memora
import memora.storage as storage
from memora import schema, tidy_eval as te
from memora.backends import LocalSQLiteBackend
from tests.l3_fakes import URI


@pytest.fixture()
def store(tmp_path, monkeypatch):
    backend = LocalSQLiteBackend(tmp_path / "memories.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    conn = backend.connect()
    schema.ensure_schema(conn)
    schema.install_sync(conn, URI, 0)
    conn.close()
    return backend


def add(content, project="alpha", meta=None, tags=None):
    md = dict(meta or {})
    if project:
        md["project"] = project
    with storage.connect() as conn:
        r = storage.add_memory(conn, content=content, tags=list(tags or []), metadata=md)
        conn.commit()
    return r["id"] if "id" in r else r["memory"]["id"]


def supersede(old, new):
    with storage.connect() as conn:
        storage.add_link(conn, new, old, edge_type="supersedes")
        conn.commit()


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_queries(tmp_path, queries):
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"queries": queries}))
    return str(p)


def run(store, tmp_path, queries, name="r.json"):
    qf = write_queries(tmp_path, queries)
    out = tmp_path / name
    rc = te.main(["--db", str(store.db_path), "--queries", qf, "--out", str(out)])
    assert rc == 0
    return json.loads(out.read_text())


@pytest.fixture()
def corpus(store):
    ids = {
        "kiwi": add("kiwi orchard irrigation schedule for the spring"),
        "plum": add("plum harvest crate labelling convention"),
        "beta": add("kiwi vine trellis wiring plan", project="beta"),
        "noise": add("completely unrelated ledger of invoices"),
    }
    return ids


def test_hit_and_miss_with_rank_and_ids(store, tmp_path, corpus):
    rep = run(store, tmp_path, [
        {"q": "kiwi orchard irrigation", "project": None, "expect": [corpus["kiwi"]]},
        {"q": "kiwi orchard irrigation", "project": "beta", "expect": [corpus["noise"]]},
        {"q": "kiwi orchard irrigation schedule", "project": None, "expect": [987654]},
    ])
    a, scoped_miss, b = rep["queries"]
    assert scoped_miss["unscoped"]["hit"] is True and scoped_miss["unscoped"]["rank"] == 4
    assert scoped_miss["scoped"]["hit"] is False and scoped_miss["scoped"]["rank"] is None
    assert a["unscoped"]["hit"] is True and a["unscoped"]["rank"] == 1 and a["unscoped"]["ids"][0] == corpus["kiwi"]
    assert "scoped" not in a
    assert b["unscoped"]["hit"] is False and b["unscoped"]["rank"] is None
    assert rep["totals"]["unscoped"] == {"n": 3, "hits": 2, "recall_at_5": 0.6667, "mrr": 0.4167}


def test_scoped_differs_from_unscoped(store, tmp_path, corpus):
    rep = run(store, tmp_path, [{"q": "kiwi", "project": "beta", "expect": [corpus["beta"]]}])
    q = rep["queries"][0]
    assert set(q["scoped"]["ids"]) == {corpus["beta"]}                 # only beta's memory
    assert corpus["kiwi"] in q["unscoped"]["ids"] and len(q["unscoped"]["ids"]) > 1
    assert rep["totals"]["scoped"]["n"] == 1 and rep["totals"]["scoped"]["recall_at_5"] == 1.0


def test_superseded_expected_id_is_hit_by_its_successor(store, tmp_path, corpus):
    new = add("kiwi orchard irrigation schedule revised for the summer")
    supersede(corpus["kiwi"], new)
    rep = run(store, tmp_path, [{"q": "kiwi orchard irrigation schedule", "project": None, "expect": [corpus["kiwi"]]}])
    q = rep["queries"][0]
    assert corpus["kiwi"] not in q["unscoped"]["ids"] and new in q["unscoped"]["ids"]
    assert q["unscoped"]["hit"] is True and q["accepted"] == [new]


def test_retired_expected_id_without_successor_is_a_miss(store, tmp_path, corpus):
    with storage.connect() as conn:
        storage.delete_memory(conn, corpus["plum"])
        conn.commit()
    rep = run(store, tmp_path, [{"q": "plum harvest crate", "project": None, "expect": [corpus["plum"]]}])
    q = rep["queries"][0]
    assert q["unscoped"]["hit"] is False and q["accepted"] == []


def test_digest_first_true_false_and_na(store, tmp_path, corpus):
    q = [{"q": "what is current for alpha", "project": "alpha", "expect": [], "digest": True}]
    assert run(store, tmp_path, q, "none.json")["queries"][0]["digest"] == {"live": [], "first": None}
    d = add("what is current for alpha: kiwi plum summary", meta={"kind": "digest"})
    rep = run(store, tmp_path, q, "yes.json")
    assert rep["queries"][0]["digest"] == {"live": [d], "first": True}
    assert rep["totals"]["digest_first"] == {"asked": 1, "true": 1, "false": 0, "n_a": 0}
    for i in range(3):
        add(f"what is current for alpha what is current for alpha what is current {i}")
    rep = run(store, tmp_path, [{"q": "what is current for alpha what is current", "project": "alpha",
                                 "expect": [], "digest": True}], "no.json")
    first = rep["queries"][0]["scoped"]["ids"][0]
    assert rep["queries"][0]["digest"]["first"] is (first == d)
    assert first != d and rep["queries"][0]["digest"]["first"] is False


def test_compare_deltas_and_totals(store, tmp_path, corpus):
    qs = [{"q": "kiwi orchard irrigation", "project": None, "expect": [corpus["kiwi"]]},
          {"q": "plum harvest crate", "project": "alpha", "expect": [corpus["plum"]]}]
    before = run(store, tmp_path, qs, "before.json")
    with storage.connect() as conn:
        storage.delete_memory(conn, corpus["plum"])
        conn.commit()
    after = run(store, tmp_path, qs, "after.json")
    c = te.compare(before, after)
    assert [d["unscoped"]["change"] for d in c["queries"]] == ["same", "lost"]
    assert c["totals"]["unscoped"]["recall_at_5"] == [1.0, 0.5, -0.5]
    assert "lost" in te.compare_text(c)
    out = tmp_path / "cmp.json"
    assert te.main(["--compare", str(tmp_path / "before.json"), str(tmp_path / "after.json"), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["totals"]["unscoped"]["recall_at_5"][2] == -0.5


def test_nothing_in_the_copy_changes(store, tmp_path, corpus):
    path = store.db_path
    before = sha_of(path)
    names = sorted(p.name for p in Path(path).parent.iterdir())
    run(store, tmp_path, [{"q": "kiwi", "project": "alpha", "expect": [corpus["kiwi"]], "digest": True}])
    assert sha_of(path) == before
    assert sorted(p.name for p in Path(path).parent.iterdir() if p.name != "q.json" and p.name != "r.json") == \
        [n for n in names if n not in ("q.json", "r.json")]
    with storage.connect() as conn:
        assert conn.execute("SELECT COALESCE(SUM(access_count),0), COUNT(last_accessed) FROM memories").fetchone()[:] == (0, 0)


def test_deterministic_and_read_ledger_untouched(store, tmp_path, corpus, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(ledger))
    qs = [{"q": "kiwi", "project": "alpha", "expect": [corpus["kiwi"]]}, {"q": "plum crate", "project": None, "expect": [corpus["plum"]]}]
    a, b = run(store, tmp_path, qs, "a.json"), run(store, tmp_path, qs, "b.json")
    for r in (a, b):
        r.pop("created")
    assert a == b
    assert not ledger.exists()


def test_embedding_unavailable_stops_instead_of_keyword_only(store, tmp_path, corpus, monkeypatch, capsys):
    monkeypatch.setattr(storage, "_query_embedding", lambda q: {})
    rc = te.main(["--db", str(store.db_path), "--queries", write_queries(tmp_path, [{"q": "kiwi", "project": None, "expect": [1]}])])
    assert rc == 2 and "embedding" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [
    {"q": "", "project": None, "expect": [1]},
    {"q": "x", "project": None, "expect": ["1"]},
    {"q": "x", "project": None, "expect": [True]},
    {"q": "x", "project": None, "expect": []},
    {"q": "x", "project": None, "expect": [], "digest": True},
    {"q": "x", "project": 3, "expect": [1]},
])
def test_bad_query_files_are_refused(tmp_path, bad):
    with pytest.raises(te.EvalError):
        te.load_queries(write_queries(tmp_path, [bad]))


def test_duplicate_pair_refused_and_example_file_loads(tmp_path):
    root = Path(__file__).resolve().parent.parent
    p = root / "scripts/tidy/queries.json.example"
    assert len(te.load_queries(str(p))) == 4
    d = json.loads(p.read_text())
    d["queries"].append(d["queries"][0])
    f = tmp_path / "dup.json"
    f.write_text(json.dumps(d))
    with pytest.raises(te.EvalError):
        te.load_queries(str(f))


def test_missing_db_and_queries_exit_2(tmp_path, capsys):
    assert te.main(["--db", str(tmp_path / "nope.db"), "--queries", write_queries(tmp_path, [{"q": "x", "project": None, "expect": [1]}])]) == 2
    assert te.main(["--db", str(tmp_path / "nope.db"), "--queries", str(tmp_path / "missing.json")]) == 2


def fwd_link(new, old):
    with storage.connect() as conn:
        storage.add_link(conn, new, old, edge_type="supersedes", bidirectional=False)
        conn.commit()


def accepted(store, mid):
    with contextlib.closing(te.dream.open_readonly_file(str(store.db_path))) as conn:
        return te.accepted_ids(conn, mid)


def test_forward_only_supersedes_accepts_only_the_successor(store, tmp_path, corpus):
    assert accepted(store, corpus["kiwi"]) == [corpus["kiwi"]]          # before: A is live
    new = add("kiwi orchard irrigation schedule newer")
    fwd_link(new, corpus["kiwi"])
    assert accepted(store, corpus["kiwi"]) == [new]
    rep = run(store, tmp_path, [{"q": "kiwi orchard irrigation schedule", "project": None, "expect": [corpus["kiwi"]]}])
    ids = rep["queries"][0]["unscoped"]["ids"]
    assert corpus["kiwi"] in ids and new in ids                         # the search still lists A
    assert rep["queries"][0]["unscoped"]["rank"] == ids.index(new) + 1   # rank is B's, not A's


def test_only_the_retired_predecessor_in_the_top_five_is_a_miss(store, tmp_path, corpus):
    new = add("zzz qqq unrelated wording entirely")
    fwd_link(new, corpus["kiwi"])
    for i in range(3):
        add(f"kiwi orchard irrigation schedule filler {i}")
    rep = run(store, tmp_path, [{"q": "kiwi orchard irrigation schedule", "project": None, "expect": [corpus["kiwi"]]}])
    u = rep["queries"][0]["unscoped"]
    assert corpus["kiwi"] in u["ids"] and new not in u["ids"]
    assert u["hit"] is False and rep["queries"][0]["accepted"] == [new]


def test_tombstoned_forward_holder_is_not_a_live_successor(store, tmp_path, corpus):
    new = add("kiwi orchard irrigation schedule newer")
    fwd_link(new, corpus["kiwi"])
    with storage.connect() as conn:         # tombstone the holder alone (a lineage delete would take A with it)
        conn.execute("INSERT INTO tombstone_components (memory_id, content_hash, reason) VALUES (?, 'h', 'test')", (new,))
        conn.commit()
    assert accepted(store, corpus["kiwi"]) == [corpus["kiwi"]]


def test_lineage_delete_of_the_holder_retires_the_predecessor_too(store, tmp_path, corpus):
    new = add("kiwi orchard irrigation schedule newer")
    fwd_link(new, corpus["kiwi"])
    with storage.connect() as conn:
        storage.delete_memory(conn, new)
        conn.commit()
    assert accepted(store, corpus["kiwi"]) == []


def test_fork_and_chain_accept_leaves(store, tmp_path, corpus):
    b, c = add("kiwi orchard irrigation v2a"), add("kiwi orchard irrigation v2b")
    d = add("kiwi orchard irrigation v3")
    supersede(corpus["kiwi"], b)
    supersede(corpus["kiwi"], c)
    supersede(b, d)
    assert accepted(store, corpus["kiwi"]) == sorted([c, d])


def test_compare_refuses_changed_criteria(store, tmp_path, corpus, capsys):
    a = run(store, tmp_path, [{"q": "kiwi", "project": None, "expect": [corpus["kiwi"]]}], "a.json")
    b = run(store, tmp_path, [{"q": "kiwi", "project": None, "expect": [corpus["plum"]]}], "b.json")
    with pytest.raises(te.EvalError):
        te.compare(a, b)
    assert te.main(["--compare", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 2
    assert "criteria changed" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [
    {"version": 1, "queries": [{}], "totals": {}},
    {"version": 1, "queries": [{"q": "x", "project": None, "expect": [], "digest_asked": False, "unscoped": 3}], "totals": {}},
    {"version": 1, "queries": [], "totals": {"unscoped": {}}},
    {"version": 1, "queries": "x", "totals": {}},
    {"version": 1, "queries": [], "totals": {"unscoped": {"recall_at_5": "x", "mrr": 1}, "scoped": {"recall_at_5": 1, "mrr": 1}}},
    {"version": 1, "queries": [{"q": "x", "project": None, "expect": [], "digest_asked": False, "unscoped": {"hit": "yes", "rank": 1}}], "totals": {}},
    [],
])
def test_malformed_reports_are_refused_not_crashed(tmp_path, bad, capsys):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad))
    assert te.main(["--compare", str(f), str(f)]) == 2


def _wal_copy(store, tmp_path, name):
    import shutil, sqlite3
    src = sqlite3.connect(store.db_path)
    src.execute("PRAGMA journal_mode=WAL")
    src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    src.close()
    d = tmp_path / name
    d.mkdir()
    shutil.copy(store.db_path, d / "m.db")                 # the main file only: no -wal, no -shm
    return d / "m.db"


def _tree(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


def test_wal_format_copy_without_sidecars_is_read_and_unchanged(store, tmp_path, corpus):
    p = _wal_copy(store, tmp_path, "w1")
    assert sorted(x.name for x in p.parent.iterdir()) == ["m.db"]
    before = _tree(p.parent)
    with contextlib.closing(te.dream.open_readonly_file(str(p))) as conn:
        rep = te.evaluate(conn, [{"q": "kiwi orchard", "project": None, "expect": [corpus["kiwi"]], "digest": False}])
    assert rep["queries"][0]["unscoped"]["hit"] is True
    assert _tree(p.parent) == before                        # no -wal/-shm created, main file untouched


def test_wal_copy_with_sidecars_is_read_or_refused_never_changed(store, tmp_path, corpus):
    import sqlite3
    p = _wal_copy(store, tmp_path, "w2")
    w = sqlite3.connect(p)                                  # an external connection: creates -wal and -shm
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("SELECT count(*) FROM memories").fetchone()
    try:
        main_before = hashlib.sha256(p.read_bytes()).hexdigest()
        wal_before = (p.parent / "m.db-wal").read_bytes()
        try:
            with contextlib.closing(te.dream.open_readonly_file(str(p))) as conn:
                rep = te.evaluate(conn, [{"q": "kiwi orchard", "project": None, "expect": [corpus["kiwi"]], "digest": False}])
            assert rep["queries"][0]["unscoped"]["hit"] is True
        except te.dream.DreamError:
            pass                                            # an explicit refusal is also acceptable
        assert hashlib.sha256(p.read_bytes()).hexdigest() == main_before
        assert (p.parent / "m.db-wal").read_bytes() == wal_before
    finally:
        w.close()


def _graph(store):
    """A, B, C, ids in a fresh corpus-free store."""
    return [add(f"kiwi orchard irrigation schedule v{i}") for i in (1, 2, 3)]


def test_all_forward_chain_reaches_the_current_leaf(store):
    a, b, c = _graph(store)
    fwd_link(b, a)
    fwd_link(c, b)
    assert accepted(store, a) == [c] and accepted(store, b) == [c]


def test_forward_first_hop_then_bidirectional(store):
    a, b, c = _graph(store)
    fwd_link(b, a)
    supersede(b, c)
    assert accepted(store, a) == [c]


def test_bidirectional_first_hop_then_forward(store):
    a, b, c = _graph(store)
    supersede(a, b)
    fwd_link(c, b)
    assert accepted(store, a) == [c]


def test_branching_forward_chain_yields_both_leaves(store):
    a, b, c = _graph(store)
    d = add("kiwi orchard irrigation schedule v4")
    fwd_link(b, a)
    fwd_link(c, b)
    fwd_link(d, b)
    assert accepted(store, a) == sorted([c, d])


def test_superseded_holder_with_live_successor(store):
    a, b, c = _graph(store)
    fwd_link(b, a)
    fwd_link(c, b)
    with storage.connect() as conn:         # C goes away alone: B is the newest live version again
        conn.execute("INSERT INTO tombstone_components (memory_id, content_hash, reason) VALUES (?, 'h', 'test')", (c,))
        conn.commit()
    assert accepted(store, a) == [b]


def test_forward_cycle_accepts_nothing(store):
    a, b, _ = _graph(store)
    fwd_link(b, a)
    fwd_link(a, b)
    assert accepted(store, a) == [] and accepted(store, b) == []
