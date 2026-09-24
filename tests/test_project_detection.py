"""Issue #47: a memory's project comes from explicit markers, never keywords.

Resolution order: an explicit `project` argument, else metadata.project, else
exactly one tag naming a project configured for the store (MEMORA_PROJECTS),
else no project. Section/subsection assignment and generic-tag prefixing act
only on a resolved project; LLM-suggested tags are filtered by the configured
tag policy, not a hardcoded memora/clmux prefix list.
"""

import asyncio
import io
import json
import sys

import pytest

import memora
import memora.storage as storage

# Modelled on memora #1082 (parked Claude Mods design) and #1109 (pi channel
# work): both mention clmux vocabulary, so the old detector put both in clmux.
PARKED_DESIGN = (
    "Claude Mods design idea (parked, not started): a mod loader that lets users "
    "bundle prompt snippets, hooks and statusline widgets. Open question: whether "
    "mods ship through clmux agent delivery or a separate registry."
)
PI_CHANNEL_WORK = (
    "pi channel work: the pi agent now receives inbox doorbells over the clmux "
    "agent delivery channel instead of pane injection."
)
GENERIC_TECH = (
    "The daemon keeps one workspace per socket and the sidebar shows embedding "
    "latency for the absorb pipeline."
)


@pytest.fixture
def projects(monkeypatch):
    def set_projects(value):
        if value is None:
            monkeypatch.delenv("MEMORA_PROJECTS", raising=False)
        else:
            monkeypatch.setenv("MEMORA_PROJECTS", json.dumps(value))
    return set_projects


@pytest.fixture(params=["local_db", "fake_d1_backend"])
def db(request, monkeypatch):
    request.getfixturevalue(request.param)
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    return request.param


def _add(conn, content, **kw):
    return storage.add_memory(conn, content=content, **kw)


# --- resolution -------------------------------------------------------------

def test_generic_technical_text_stays_unclassified(db, projects):
    projects(["memora", "clmux", "pi"])
    with storage.connect() as conn:
        rec = _add(conn, GENERIC_TECH, tags=["architecture"])
    assert rec["tags"] == ["architecture"]
    assert not (rec["metadata"] or {}).get("section")
    assert "project" not in (rec["metadata"] or {})


def test_1082_and_1109_no_longer_share_a_project_by_keyword(db, projects):
    projects(["memora", "clmux", "pi"])
    assert storage._resolve_project(None, [], None) is None
    with storage.connect() as conn:
        old = _add(conn, PARKED_DESIGN, tags=["architecture"])
        new = _add(conn, PI_CHANNEL_WORK, tags=["architecture"], project="pi")
    assert old["tags"] == ["architecture"] and not (old["metadata"] or {}).get("section")
    assert new["tags"] == ["pi/architecture"]
    assert new["metadata"]["section"] == "pi" and new["metadata"]["project"] == "pi"


def test_multi_project_store_resolves_each_memory_from_its_own_markers(db, projects):
    projects(["memora", "clmux", "pi"])
    with storage.connect() as conn:
        by_tag = _add(conn, "sidebar refresh cadence", tags=["clmux/tui", "design"])
        by_meta = _add(conn, "doorbell over channel", metadata={"project": "pi"}, tags=["research"])
        explicit = _add(conn, "absorb gate calibration", tags=["analysis"], project="memora")
        ambiguous = _add(conn, "shared note", tags=["clmux/tui", "pi/channels", "plan"])
    assert by_tag["metadata"]["section"] == "clmux" and by_tag["metadata"]["subsection"] == "tui"
    assert "clmux/design" in by_tag["tags"]
    assert by_meta["metadata"]["section"] == "pi" and by_meta["tags"] == ["pi/research"]
    assert explicit["metadata"]["section"] == "memora" and explicit["tags"] == ["memora/analysis"]
    # Two configured projects in the tags: no guess.
    assert not (ambiguous["metadata"] or {}).get("section") and "plan" in ambiguous["tags"]


def test_unconfigured_store_infers_nothing_from_tags_but_accepts_explicit(db, projects):
    projects(None)
    with storage.connect() as conn:
        tagged = _add(conn, "sidebar refresh", tags=["clmux/tui", "design"])
        explicit = _add(conn, "sidebar refresh", tags=["design"], project="clmux")
    assert tagged["tags"] == ["clmux/tui", "design"] and not (tagged["metadata"] or {}).get("section")
    assert explicit["tags"] == ["clmux/design"] and explicit["metadata"]["section"] == "clmux"


def test_explicit_project_outside_the_configured_list_is_rejected(db, projects):
    projects(["memora", "clmux"])
    with storage.connect() as conn:
        with pytest.raises(storage.ProjectConfigError):
            _add(conn, "x y z note", project="pi")
        with pytest.raises(ValueError):
            _add(conn, "x y z note", project="Not A Name")


def test_per_store_projects_config(db, projects, monkeypatch):
    projects({"default": ["pi"], "other": ["memora"]})
    monkeypatch.setattr(storage, "effective_database_name", lambda: None)
    assert storage.configured_projects() == ("pi",)
    assert storage.configured_projects("other") == ("memora",)
    assert storage.configured_projects("missing") == ()


def test_malformed_projects_config_fails_loudly(projects):
    projects({"default": ["Bad Name"]})
    with pytest.raises(storage.ProjectConfigError):
        storage.configured_projects("default")


def test_update_memory_keeps_prefixing_on_its_explicit_project(db, projects):
    projects(["memora", "clmux", "pi"])
    with storage.connect() as conn:
        rec = _add(conn, "pi note", tags=["plan"], project="pi")
        updated = storage.update_memory(conn, rec["id"], tags=["design"])
    assert updated["tags"] == ["pi/design"]


# --- suggested tags -----------------------------------------------------------

def test_allowlisted_third_project_tags_survive_filtering(monkeypatch, projects):
    projects(["memora", "clmux", "pi"])
    monkeypatch.setattr(memora, "TAG_WHITELIST", {"pi/*", "memora/*"})
    kept = storage._filter_suggested_tags(["pi/research", "clmux/architecture", "memora/notes", "bare"])
    assert kept == ["pi/research", "memora/notes"]  # clmux/* not allowed; bare not project-prefixed


def test_allow_any_tag_keeps_any_prefixed_suggestion(monkeypatch, projects):
    projects(None)
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    assert storage._filter_suggested_tags(["trader/strategy", "pi/channels", "x"]) == [
        "trader/strategy", "pi/channels",
    ]


def test_suggestions_naming_another_configured_project_are_dropped(monkeypatch, projects):
    projects(["memora", "clmux", "pi"])
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    assert storage._filter_suggested_tags(
        ["clmux/architecture", "pi/channels", "other/x"], project="pi",
    ) == ["pi/channels", "other/x"]


# --- absorb -----------------------------------------------------------------

def test_explicit_project_on_absorb_drives_section_and_tag_prefixing(db, projects, monkeypatch):
    projects(["memora", "clmux", "pi"])
    monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
    # The classifier suggests a clmux tag for pi work (what happened to #1109).
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])
    with storage.connect() as conn:
        result = storage.absorb_memory(
            conn, [PI_CHANNEL_WORK], tags=["architecture"], project="pi",
        )
        (decision,) = result["decisions"]
        mem = storage.get_memory(conn, decision["memory_id"])
    assert mem["tags"] == ["pi/architecture"]
    assert mem["metadata"]["section"] == "pi" and mem["metadata"]["project"] == "pi"


def test_absorb_drops_suggested_tags_of_another_project(db, projects, monkeypatch):
    projects(["memora", "clmux", "pi"])
    monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
    with storage.connect() as conn:
        seed = _add(conn, "pi inbox baseline", tags=["pi/channels"], project="pi")
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [{"score": 0.6, "memory": seed}])
    monkeypatch.setattr(
        storage, "_classify_fact_against_matches",
        lambda fact, matches: ([{"memory_id": seed["id"], "relationship": "RELATED", "reason": "r"}],
                               ["clmux/architecture", "pi/research"]),
    )
    with storage.connect() as conn:
        result = storage.absorb_memory(conn, [PI_CHANNEL_WORK], project="pi")
        (decision,) = result["decisions"]
        mem = storage.get_memory(conn, decision["memory_id"])
    assert "clmux/architecture" not in mem["tags"] and "pi/research" in mem["tags"]


def test_absorb_rejects_an_unknown_project_before_any_work(db, projects, monkeypatch):
    projects(["memora"])
    monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: pytest.fail("no work"))
    with storage.connect() as conn:
        with pytest.raises(storage.ProjectConfigError):
            storage.absorb_memory(conn, ["some fact here"], project="pi")


def test_classify_prompt_no_longer_seeds_memora_or_clmux(monkeypatch):
    seen = {}

    class Completions:
        def create(self, **kw):
            seen["prompt"] = kw["messages"][-1]["content"]
            raise RuntimeError("stop")

    from types import SimpleNamespace
    monkeypatch.setattr(storage, "_get_llm_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    storage._classify_fact_against_matches("fact", [{"id": 1, "content": "c", "score": 0.5, "tags": []}])
    assert "memora/research" not in seen["prompt"] and "clmux/architecture" not in seen["prompt"]
    assert "Do not guess a project" in seen["prompt"]


# --- MCP tools and CLI ----------------------------------------------------------

def test_mcp_tools_accept_project(db, projects, monkeypatch):
    from memora import server

    projects(["memora", "clmux", "pi"])
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])
    created = asyncio.run(server.memory_create("pi mailbox note", tags=["plan"], project="pi"))
    assert created["memory"]["tags"] == ["pi/plan"]
    issue = asyncio.run(server.memory_create_issue("pi bug", project="pi"))
    assert issue["memory"]["tags"] == ["pi/issues"] and issue["memory"]["metadata"]["project"] == "pi"
    todo = asyncio.run(server.memory_create_todo("pi task", project="pi"))
    assert todo["memory"]["tags"] == ["pi/todos"]
    bad = asyncio.run(server.memory_create("x y z", project="nope"))
    assert bad["error"] == "invalid_input"
    absorbed = asyncio.run(server.memory_absorb(["pi fact number one"], project="pi", dry_run=True))
    assert "error" not in absorbed
    rejected = asyncio.run(server.memory_absorb(["pi fact"], project="nope"))
    assert rejected["error"] == "invalid_input"


def test_cli_absorb_project_flag(db, projects, monkeypatch, capsys):
    from memora import cli

    projects(["pi"])
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])
    monkeypatch.setattr(sys, "stdin", io.StringIO("cli pi fact text"))
    monkeypatch.setattr(sys, "argv", ["memora.cli", "absorb", "--project", "pi", "--tags", "plan"])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    with storage.connect() as conn:
        mem = storage.get_memory(conn, out["decisions"][0]["memory_id"])
    assert mem["tags"] == ["pi/plan"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("cli pi fact text two"))
    monkeypatch.setattr(sys, "argv", ["memora.cli", "absorb", "--project", "nope"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_input"


# --- dry-run report -------------------------------------------------------------

def test_report_lists_memories_the_keywords_would_have_classified(db, projects, capsys):
    sys.path.insert(0, "scripts")
    import report_project_detection as report

    projects(["memora", "clmux", "pi"])
    with storage.connect() as conn:
        # As the OLD code would have stored it: keyword -> clmux section + prefix.
        guessed = _add(conn, "The daemon keeps one workspace per socket; the sidebar lags.",
                       metadata={"section": "clmux"}, tags=["clmux/architecture"])
        explicit = _add(conn, "pi mailbox", tags=["plan"], project="pi")
        plain = _add(conn, "a recipe for bread", tags=["plan"])
        before = conn.execute("SELECT COUNT(*), MAX(id) FROM memories").fetchone()
    assert report.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    ids = {m["id"]: m for m in data["memories"]}
    assert guessed["id"] in ids and explicit["id"] not in ids and plain["id"] not in ids
    row = ids[guessed["id"]]
    assert row["old_keyword_project"] == "clmux" and row["new_project"] is None
    assert row["changed"]["section"] == {"stored": "clmux", "new": None}
    assert row["changed"]["tags"] == {"stored": ["clmux/architecture"], "new": ["architecture"]}
    assert "clmux" in row["keyword_indicators"]
    assert data["summary"]["reported"] == 1
    assert report.main(["--json", "--all"]) == 0
    everything = json.loads(capsys.readouterr().out)
    assert everything["summary"]["reported"] >= 1 and plain["id"] not in {
        m["id"] for m in everything["memories"]
    }
    with storage.connect() as conn:  # read-only: nothing changed
        assert conn.execute("SELECT COUNT(*), MAX(id) FROM memories").fetchone() == before
        assert storage.get_memory(conn, guessed["id"])["tags"] == ["clmux/architecture"]


# --- round 2: metadata.project obeys the same rule (review 7030 HIGH 1) --------

def test_metadata_project_cannot_bypass_the_configured_list(db, projects):
    from memora import server

    projects(["memora"])
    with storage.connect() as conn:
        with pytest.raises(storage.ProjectConfigError):
            _add(conn, "a pi fact", metadata={"project": "pi"}, tags=["analysis"])
        # import: the entry fails, nothing tagged pi/ is written
        result = storage.import_memories(
            conn, [{"content": "imported pi fact", "metadata": {"project": "pi"}, "tags": ["analysis"]}],
        )
        assert result.get("errors") and not any(
            "pi/analysis" in (m.get("tags") or []) for m in storage.list_memories(conn)
        )
    bad = asyncio.run(server.memory_create("a pi fact", metadata={"project": "pi"}, tags=["analysis"]))
    assert bad["error"] == "invalid_input"


def test_update_rejects_a_supplied_project_but_tolerates_a_stored_one(db, projects):
    projects(None)
    with storage.connect() as conn:
        legacy = _add(conn, "legacy memory", metadata={"project": "target"}, tags=["plan"])
    projects(["memora"])
    with storage.connect() as conn:
        with pytest.raises(storage.ProjectConfigError):
            storage.update_memory(conn, legacy["id"], metadata={"project": "pi"})
        # The stored legacy value is not configured: tolerated, but no prefixing.
        updated = storage.update_memory(conn, legacy["id"], tags=["design"])
    assert updated["tags"] == ["design"]


# --- round 2: typed tags follow the project, no memora default (HIGH 2) --------

def test_typed_tags_follow_the_project_with_no_memora_default(db, projects, monkeypatch):
    from memora import server

    projects(["memora", "clmux", "pi"])
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])
    assert asyncio.run(server.memory_create_issue("an issue"))["memory"]["tags"] == ["issues"]
    assert asyncio.run(server.memory_create_todo("a task"))["memory"]["tags"] == ["todos"]
    assert asyncio.run(server.memory_create_section("Arch"))["memory"]["tags"] == ["sections"]
    section = asyncio.run(server.memory_create_section("Arch", project="pi"))["memory"]
    assert section["tags"] == ["pi/sections"] and section["metadata"]["project"] == "pi"


def test_documents_take_the_project(db, projects):
    from memora import server

    projects(["memora", "pi"])
    doc = "# Plan\n\n1. first step\n2. second step\n"
    out = asyncio.run(server.memory_store_document(doc, "pi/plan-doc", project="pi"))
    with storage.connect() as conn:
        root = storage.get_memory(conn, out["root_id"])
        frags = [storage.get_memory(conn, i) for ids in out["node_map"].values() for i in ids]
    assert "pi/documents" in root["tags"] and root["metadata"]["project"] == "pi"
    assert frags and all("pi/documents" in f["tags"] and f["metadata"]["project"] == "pi" for f in frags)
    plain = asyncio.run(server.memory_store_document(doc, "plain-doc"))
    with storage.connect() as conn:
        assert "documents" in storage.get_memory(conn, plain["root_id"])["tags"]
        assert "memora/documents" not in storage.get_memory(conn, plain["root_id"])["tags"]


def test_create_suggestions_use_the_memorys_own_project(db, projects, monkeypatch):
    from memora import server

    projects(["memora", "pi"])
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])
    pi = asyncio.run(server.memory_create("TODO: wire the pi inbox", project="pi"))
    assert pi["suggestions"]["tags"] == ["pi/todos"]
    none = asyncio.run(server.memory_create("TODO: something generic"))
    assert none["suggestions"]["tags"] == ["todos"]
    # memora-tagged content still resolves to memora under MEMORA_PROJECTS.
    mem = asyncio.run(server.memory_create("TODO: absorb gate", tags=["memora/absorb"]))
    assert mem["suggestions"]["tags"] == ["memora/todos"]


def test_graph_issue_filter_accepts_any_project_issues_tag(graph_request, projects):
    projects(["pi"])
    with storage.connect() as conn:
        tagged = _add(conn, "tag-only pi issue", tags=["pi/issues"])
        bare = _add(conn, "tag-only bare issue", tags=["issues"])
        other = _add(conn, "not an issue", tags=["pi/notes"])
    status, api = graph_request("GET", "/api/memories?type=issue&limit=50")
    assert status == 200
    ids = {m["id"] for m in api.get("memories", api if isinstance(api, list) else [])}
    assert {tagged["id"], bare["id"]} <= ids and other["id"] not in ids


# --- round 2: full MEMORA_PROJECTS validation at startup ------------------------

@pytest.mark.parametrize("raw", [
    '{"memora": ["memora"], "unused": ["Bad Name"]}',
    '{"memora": "memora"}',
    '{"Bad Store": ["memora"]}',
    '"memora"',
    '["ok", 3]',
    "not json",
])
def test_malformed_projects_config_fails_at_startup(monkeypatch, raw, capsys):
    from memora import server

    monkeypatch.setenv("MEMORA_PROJECTS", raw)
    with pytest.raises(storage.ProjectConfigError):
        storage.load_projects_config()
    with pytest.raises(SystemExit) as exit_info:
        server.main(["--transport", "stdio"])
    assert exit_info.value.code == 2
    assert "MEMORA_PROJECTS" in capsys.readouterr().err


def test_report_says_it_is_a_preview_not_the_backfill(db, projects, capsys):
    sys.path.insert(0, "scripts")
    import report_project_detection as report

    projects(["clmux"])
    assert report.main([]) == 0
    out = capsys.readouterr().out
    assert "REMEDIATION PREVIEW" in out and "backfill_tags does NOT perform" in out
    assert report.main(["--json"]) == 0
    assert "does NOT perform" in json.loads(capsys.readouterr().out)["summary"]["kind"]


# --- round 3: typed tags under the DEFAULT tag policy (review 7040 HIGH 1) ------

@pytest.fixture(params=["local_db", "fake_d1_backend"])
def default_policy_db(request, monkeypatch):
    """The real out-of-the-box policy: memora.DEFAULT_TAGS, no ALLOW_ANY."""
    request.getfixturevalue(request.param)
    monkeypatch.setattr(memora, "TAG_WHITELIST", set(memora.DEFAULT_TAGS))
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])
    return request.param


def test_typed_tools_work_under_the_default_policy(default_policy_db, projects):
    from memora import server

    projects(["pi"])
    for tool, kind in ((server.memory_create_issue, "issues"), (server.memory_create_todo, "todos"),
                       (server.memory_create_section, "sections")):
        bare = asyncio.run(tool("typed thing"))
        assert "error" not in bare, bare
        assert bare["memory"]["tags"] == [kind]
        pi = asyncio.run(tool("typed pi thing", project="pi"))
        assert "error" not in pi, pi
        assert pi["memory"]["tags"] == [f"pi/{kind}"]


def test_user_supplied_tags_are_still_enforced_under_the_default_policy(default_policy_db, projects):
    from memora import server

    projects(["pi"])
    # A caller cannot hand-apply a typed tag: only memora's own are exempt.
    denied = asyncio.run(server.memory_create("x y z", tags=["pi/issues"]))
    assert denied["error"] == "invalid_input"
    denied = asyncio.run(server.memory_create("x y z", tags=["not-allowed"]))
    assert denied["error"] == "invalid_input"
    with storage.connect() as conn:
        with pytest.raises(ValueError):  # a system tag for ANOTHER project
            _add(conn, "x y z", project="pi", system_tags=["clmux/issues"])
        with pytest.raises(ValueError):  # not a typed kind
            _add(conn, "x y z", system_tags=["anything"])


def test_explicit_project_keeps_allowed_generic_tags_bare_under_the_default_policy(default_policy_db, projects):
    projects(["pi"])
    with storage.connect() as conn:
        rec = _add(conn, "pi plan text", tags=["plan", "analysis"], project="pi")
    # pi/plan is not in the default policy: stays "plan" instead of failing.
    assert rec["tags"] == ["plan", "analysis"] and rec["metadata"]["section"] == "pi"


def test_documents_work_under_the_default_policy(default_policy_db, projects):
    from memora import server

    projects(["pi"])
    doc = "# Plan\n\n1. first step\n2. second step\n"
    out = asyncio.run(server.memory_store_document(doc, "pi/default-policy", project="pi"))
    assert "error" not in out, out
    with storage.connect() as conn:
        assert "pi/documents" in storage.get_memory(conn, out["root_id"])["tags"]


# --- round 3: documents resolve their project before the plan (HIGH 2) ----------

@pytest.mark.parametrize("how", ["metadata", "tag"])
def test_document_project_inferred_from_metadata_or_tag(db, projects, how):
    from memora import server

    projects(["memora", "pi"])
    doc = "# Plan\n\n1. first step\n2. second step\n"
    kwargs = {"metadata": {"project": "pi"}} if how == "metadata" else {"tags": ["pi/notes"]}
    out = asyncio.run(server.memory_store_document(doc, f"inferred-{how}", **kwargs))
    assert "error" not in out, out
    with storage.connect() as conn:
        root = storage.get_memory(conn, out["root_id"])
        frags = [storage.get_memory(conn, i) for ids in out["node_map"].values() for i in ids]
    for mem in [root, *frags]:
        assert "pi/documents" in mem["tags"] and "documents" not in mem["tags"], mem["tags"]


def test_document_rejects_an_unconfigured_metadata_project(db, projects):
    from memora import server

    projects(["memora"])
    out = asyncio.run(server.memory_store_document("# T\n\ntext\n", "bad", metadata={"project": "pi"}))
    assert out["error"] == "invalid_input"


# --- round 3: the digest recognises any typed tag (MEDIUM) ------------------------

def test_digest_buckets_include_tag_only_typed_entries(db, projects):
    from memora import server

    projects(None)
    with storage.connect() as conn:
        entries = {
            "todos": _add(conn, "routing digest todo bare", tags=["todos", "routing"]),
            "pi/todos": _add(conn, "routing digest todo pi", tags=["pi/todos", "routing"]),
            "pi/issues": _add(conn, "routing digest issue pi", tags=["pi/issues", "routing"]),
            "memora/issues": _add(conn, "routing digest issue legacy", tags=["memora/issues", "routing"]),
            "issues": _add(conn, "routing digest issue bare", tags=["issues", "routing"]),
        }
        noise = _add(conn, "routing digest plain note", tags=["routing"])
    digest = asyncio.run(server.memory_digest("routing digest", k=20))
    todo_ids = {item["id"] for item in digest["todos"]}
    issue_ids = {item["id"] for item in digest["issues"]}
    assert {entries["todos"]["id"], entries["pi/todos"]["id"]} <= todo_ids
    assert {entries["pi/issues"]["id"], entries["memora/issues"]["id"], entries["issues"]["id"]} <= issue_ids
    assert noise["id"] not in todo_ids | issue_ids


# --- round 4: system tags are internal-only and round-trip (review 7044) ---------

def test_public_batch_cannot_smuggle_system_tags(default_policy_db, projects):
    from memora import server

    projects(["pi"])
    out = asyncio.run(server.memory_create_batch([
        {"content": "hand-applied typed tag", "project": "pi",
         "metadata": {"type": "issue"}, "system_tags": ["pi/issues"]},
    ]))
    assert out["error"] == "invalid_batch"
    with storage.connect() as conn:
        assert not storage.list_memories(conn)
    # The internal typed tools still work under the default policy.
    issue = asyncio.run(server.memory_create_issue("real issue", project="pi"))
    assert issue["memory"]["tags"] == ["pi/issues"]


def test_system_tags_are_bound_to_metadata_type(default_policy_db, projects):
    projects(["pi"])
    with storage.connect() as conn:
        with pytest.raises(ValueError):
            _add(conn, "a todo, not an issue", project="pi",
                 metadata={"type": "todo"}, system_tags=["pi/issues"])
        with pytest.raises(ValueError):
            _add(conn, "a note", project="pi", system_tags=["pi/documents"])


def test_update_keeps_a_typed_tag_but_rejects_a_foreign_one(default_policy_db, projects):
    from memora import server

    projects(["pi"])
    issue = asyncio.run(server.memory_create_issue("editable issue", project="pi"))["memory"]
    with storage.connect() as conn:
        kept = storage.update_memory(conn, issue["id"], content="edited issue", tags=["pi/issues", "plan"])
        assert set(kept["tags"]) == {"pi/issues", "plan"}
        with pytest.raises(ValueError):  # a typed tag this issue never had
            storage.update_memory(conn, issue["id"], tags=["pi/issues", "pi/todos"])
        note = _add(conn, "plain note", project="pi", tags=["plan"])
        with pytest.raises(ValueError):  # hand-applying a typed tag via update
            storage.update_memory(conn, note["id"], tags=["plan", "pi/issues"])
    via_tool = asyncio.run(server.memory_update(issue["id"], tags=["pi/issues", "note"]))
    assert "error" not in via_tool, via_tool


def _typed_fixture(server):
    asyncio.run(server.memory_create_issue("rt issue", project="pi"))
    asyncio.run(server.memory_create_todo("rt todo"))
    asyncio.run(server.memory_create_section("rt section", project="pi"))
    asyncio.run(server.memory_store_document("# RT\n\n1. one\n2. two\n", "rt-doc", project="pi"))
    with storage.connect() as conn:
        _add(conn, "rt plain", tags=["plan"])


def _tags_by_content(conn):
    return {m["content"]: sorted(m["tags"]) for m in storage.list_memories(conn, limit=-1)}


def test_export_import_round_trips_typed_tags(default_policy_db, projects):
    from memora import server

    projects(["pi"])
    _typed_fixture(server)
    with storage.connect() as conn:
        before = _tags_by_content(conn)
        exported = storage.export_memories(conn)
        assert any(r["system_tags"] == ["pi/issues"] for r in exported)
        result = storage.import_memories(conn, exported, strategy="replace")
        assert result["replaced"] is True and result["total_errors"] == 0, result
        assert _tags_by_content(conn) == before


def test_replace_import_with_one_bad_entry_deletes_nothing(default_policy_db, projects):
    from memora import server

    projects(["pi"])
    _typed_fixture(server)
    with storage.connect() as conn:
        before = _tags_by_content(conn)
        exported = storage.export_memories(conn)
        bad = exported + [{"content": "bad entry", "tags": ["not-allowed"]}]
        result = storage.import_memories(conn, bad, strategy="replace")
        assert result["replaced"] is False and result["imported"] == 0 and result["total_errors"] == 1
        assert _tags_by_content(conn) == before  # nothing deleted, nothing added


def test_import_refuses_forged_system_tags(default_policy_db, projects):
    projects(["pi"])
    with storage.connect() as conn:
        result = storage.import_memories(conn, [
            {"content": "not really an issue", "metadata": {"type": "note"},
             "tags": ["pi/issues"], "system_tags": ["pi/issues"], "project": "pi"},
        ])
        assert result["imported"] == 0 and result["total_errors"] == 1
        assert not storage.list_memories(conn)


# --- round 5: a replace import is atomic on SQLite and truthful on D1 (review 7053) ---

PREV = [{"content": f"previous row {i}", "tags": ["plan"]} for i in range(3)]
NEW = [{"content": f"new row {i}", "tags": ["plan"]} for i in range(3)]


def _contents(conn):
    # What the STORE holds (raw rows): reads hide import-pending rows, and
    # these tests are about what is really left behind.
    return sorted(r[0] for r in conn.execute("SELECT content FROM memories").fetchall())


def _embedded_ids(conn):
    return {int(r[0]) for r in conn.execute("SELECT memory_id FROM memories_embeddings").fetchall()}


def _setup_previous(conn):
    assert storage.import_memories(conn, PREV)["imported"] == 3
    return _contents(conn)


def test_sqlite_replace_rolls_back_on_an_insert_failure(local_db, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        before = _setup_previous(conn)
        conn.execute(
            "CREATE TRIGGER fail_row BEFORE INSERT ON memories WHEN NEW.content = 'new row 1' "
            "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
        )
        result = storage.import_memories(conn, NEW, strategy="replace")
        assert result["replaced"] is False and result["imported"] == 0 and result["total_errors"] == 1
        assert _contents(conn) == before  # untouched: the DELETEs rolled back too


def test_sqlite_replace_rolls_back_on_an_embedding_failure(local_db, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        before = _setup_previous(conn)
        real, calls = storage._upsert_embedding, {"n": 0}

        def flaky(c, mid, vec):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected embedding failure")
            return real(c, mid, vec)

        monkeypatch.setattr(storage, "_upsert_embedding", flaky)
        result = storage.import_memories(conn, NEW, strategy="replace")
        assert result["replaced"] is False and result["imported"] == 0
        assert _contents(conn) == before
        assert {m["id"] for m in storage.list_memories(conn, limit=-1)} <= _embedded_ids(conn)


def _fail_nth(conn, predicate, nth, *, transient=False):
    seen = {"n": 0, "failed": 0}

    def fail_when(sql, params):
        if not predicate(sql):
            return False
        seen["n"] += 1
        if seen["n"] < nth:
            return False
        if transient and seen["failed"] >= 1:
            return False
        seen["failed"] += 1
        return True

    conn.fail_when = fail_when
    return seen


def _is_memory_insert(sql):
    return sql.lstrip().startswith("INSERT INTO memories (")


def _is_embedding_write(sql):
    return "memories_embeddings" in sql and sql.lstrip().upper().startswith(("INSERT", "UPDATE"))


def test_d1_replace_insert_failure_is_reported_partial_with_ids(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        _setup_previous(conn)
        _fail_nth(conn, _is_memory_insert, 2)  # the second new row never inserts
        result = storage.import_memories(conn, NEW, strategy="replace")
        conn.fail_when = None
        assert result["replaced"] == "partial" and result["imported"] == 1
        assert result["failed"] == 2 and len(result["written_ids"]) == 1 and result["total_errors"] == 1
        assert "not atomic" in result["message"] and "export file" in result["message"]
        # The documented state: exactly the written rows, each with its embedding.
        assert _contents(conn) == ["new row 0"]
        assert {m["id"] for m in storage.list_memories(conn, limit=-1)} == set(result["written_ids"])
        assert set(result["written_ids"]) <= _embedded_ids(conn)


def test_d1_replace_embedding_failure_leaves_no_unembedded_row(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        _setup_previous(conn)
        _fail_nth(conn, _is_embedding_write, 2)  # row two's vector never lands
        result = storage.import_memories(conn, NEW, strategy="replace")
        conn.fail_when = None
        assert result["replaced"] == "partial" and result["imported"] == 1 and result["failed"] == 2
        assert _contents(conn) == ["new row 0"]  # row two's memory was removed, not left bare
        assert {m["id"] for m in storage.list_memories(conn, limit=-1)} <= _embedded_ids(conn)


def test_d1_replace_transient_failures_are_retried_to_success(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        _setup_previous(conn)
        _fail_nth(conn, _is_embedding_write, 2, transient=True)
        result = storage.import_memories(conn, NEW, strategy="replace")
        conn.fail_when = None
        assert result["replaced"] is True and result["imported"] == 3 and result["total_errors"] == 0
        assert _contents(conn) == ["new row 0", "new row 1", "new row 2"]  # no duplicate from the retry
        assert {m["id"] for m in storage.list_memories(conn, limit=-1)} <= _embedded_ids(conn)


def test_replace_never_reports_done_with_errors(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        _setup_previous(conn)
        _fail_nth(conn, _is_memory_insert, 1)
        result = storage.import_memories(conn, NEW, strategy="replace")
        conn.fail_when = None
    assert not (result["replaced"] is True and result["total_errors"])


# --- round 6: D1 adoption only by this import's marker; staged replace clear (7063) ---

def _row_by_id(conn, mid):
    return conn.execute("SELECT content, metadata, tags FROM memories WHERE id = ?", (mid,)).fetchone()


def test_d1_append_never_adopts_a_preexisting_same_content_row(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        original = _add(conn, "duplicate text", tags=["plan"], metadata={"k": 1})
        before = _row_by_id(conn, original["id"])
        result = storage.import_memories(conn, [{"content": "duplicate text", "tags": ["analysis"]}])
        assert result["imported"] == 1 and result["total_errors"] == 0
        rows = conn.execute("SELECT id, tags, metadata FROM memories WHERE content = 'duplicate text'").fetchall()
        assert len(rows) == 2  # the requested new row exists
        assert tuple(_row_by_id(conn, original["id"])) == tuple(before)  # original untouched
        new = next(r for r in rows if r[0] != original["id"])
        assert json.loads(new[1]) == ["analysis"] and "import_attempt" not in (new[2] or "")


def test_d1_append_embedding_failure_preserves_the_preexisting_row(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        original = _add(conn, "duplicate text", tags=["plan"])
        before = _row_by_id(conn, original["id"])
        embedded_before = _embedded_ids(conn)
        conn.fail_when = lambda sql, params: _is_embedding_write(sql)  # every new vector fails
        result = storage.import_memories(conn, [{"content": "duplicate text", "tags": ["analysis"]}])
        conn.fail_when = None
        assert result["imported"] == 0 and result["total_errors"] == 1 and "replaced" not in result
        rows = conn.execute("SELECT id FROM memories WHERE content = 'duplicate text'").fetchall()
        assert [r[0] for r in rows] == [original["id"]]  # only the original, untouched
        assert tuple(_row_by_id(conn, original["id"])) == tuple(before)
        assert original["id"] in _embedded_ids(conn) and _embedded_ids(conn) == embedded_before


def test_d1_genuinely_lost_insert_is_adopted_by_marker_without_duplicate(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_execute = conn.execute
        state = {"lost": False}

        def execute(sql, params=None):
            cur = real_execute(sql, params)
            if _is_memory_insert(sql) and not state["lost"]:
                state["lost"] = True  # committed, but the response never arrives
                raise RuntimeError("response lost after commit")
            return cur

        monkeypatch.setattr(conn, "execute", execute)
        result = storage.import_memories(conn, [{"content": "lost insert text", "tags": ["plan"]}])
        monkeypatch.undo()
        assert result["imported"] == 1 and result["total_errors"] == 0
        rows = conn.execute("SELECT id, metadata FROM memories WHERE content = 'lost insert text'").fetchall()
        assert len(rows) == 1 and "import_attempt" not in (rows[0][1] or "")
        assert rows[0][0] in _embedded_ids(conn)


@pytest.mark.parametrize("stage,sql", [
    ("embeddings", "DELETE FROM memories_embeddings"),
    ("memories", "DELETE FROM memories"),
])
def test_d1_replace_clear_failure_is_reported_and_rerunnable(fake_d1_backend, monkeypatch, stage, sql):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        before = _setup_previous(conn)
        conn.fail_when = lambda s, p: s.strip() == sql
        result = storage.import_memories(conn, NEW, strategy="replace")
        conn.fail_when = None
        assert result["replaced"] == "partial" and result["clear_stage"] == stage
        assert result["imported"] == 0 and result["written_ids"] == [] and result["failed"] == 3
        assert "Re-run the same replace" in result["message"]
        assert _contents(conn) == before  # memories are cleared last: all still present
        rerun = storage.import_memories(conn, NEW, strategy="replace")
        assert rerun["replaced"] is True and _contents(conn) == ["new row 0", "new row 1", "new row 2"]
        assert {m["id"] for m in storage.list_memories(conn, limit=-1)} <= _embedded_ids(conn)


# --- round 7: verified cleanup, stale-marker sweep, pending rows hidden (7073) ---

import time  # noqa: E402


def _is_cleanup_delete(sql):
    return sql.lstrip().startswith("DELETE FROM memories WHERE id = ? AND json_extract")


def _raw_row(conn, mid):
    return conn.execute("SELECT id, metadata FROM memories WHERE id = ?", (mid,)).fetchone()


def _mark(conn, mid, *, age_s, embedded=True, import_id="0" * 32):
    """Leave `mid` as an interrupted import would: marked, with or without its vector."""
    raw = conn.execute("SELECT metadata FROM memories WHERE id = ?", (mid,)).fetchone()[0]
    meta = json.loads(raw) if raw else {}
    meta["import_attempt"] = storage._import_marker(import_id, int(time.time() - age_s), 0)
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(meta), mid))
    if not embedded:
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (mid,))
    conn.commit()
    storage.invalidate_corpus_cache(conn)


def test_d1_failed_cleanup_is_reported_as_orphan_and_swept(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        # Every vector write fails persistently, and so does the cleanup DELETE.
        conn.fail_when = lambda sql, params: _is_embedding_write(sql) or _is_cleanup_delete(sql)
        result = storage.import_memories(conn, [{"content": "orphaned import row", "tags": ["plan"]}])
        conn.fail_when = None
        assert result["imported"] == 0 and result["written_ids"] == []
        [orphan] = result["orphan_ids"]
        assert orphan["marker"].count(":") == 2
        assert "WITHOUT an embedding" in result["message"] and "must be cleaned" in result["message"]
        assert "exactly" not in result["message"]  # no exactness claim
        # The row IS present, without its vector...
        assert _raw_row(conn, orphan["id"]) is not None and orphan["id"] not in _embedded_ids(conn)
        # ...and hidden from reads until swept.
        assert storage.get_memory(conn, orphan["id"]) is None
        assert storage.list_memories(conn, limit=-1) == []
        assert storage.list_memories(conn, query="orphaned") == []
        assert storage.hybrid_search(conn, "orphaned import row") == []
        # A sweep while the marker is young leaves it (its import may still run)...
        assert storage.sweep_import_markers(conn)["pending"] == 1
        # ...and removes it once stale.
        swept = storage.sweep_import_markers(conn, now=time.time() + 601)
        assert swept["removed"] == 1 and swept["failed"] == []
        assert _raw_row(conn, orphan["id"]) is None


def test_d1_cleanup_deletes_the_memory_before_its_vector(fake_d1_backend, monkeypatch):
    """Memory row first: a failing vector DELETE after it leaves no unembedded memory."""
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_upsert = storage._upsert_embedding

        def upsert_then_fail(c, mid, vec):
            real_upsert(c, mid, vec)  # the vector lands, the response is an error
            raise RuntimeError("injected embedding failure")

        monkeypatch.setattr(storage, "_upsert_embedding", upsert_then_fail)
        conn.fail_when = lambda sql, params: sql.lstrip().startswith("DELETE FROM memories_embeddings WHERE memory_id")
        result = storage.import_memories(conn, [{"content": "half cleaned row", "tags": ["plan"]}])
        conn.fail_when = None
        assert "orphan_ids" not in result and "exactly" in result["message"]
        assert _contents(conn) == []  # the memory is gone; only a harmless stray vector may remain


def test_interrupted_import_rows_are_completed_or_removed_by_the_next_import(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        embedded = _add(conn, "stale embedded row", tags=["plan"], metadata={"k": 1})["id"]
        bare = _add(conn, "stale unembedded row", tags=["plan"])["id"]
        young = _add(conn, "young unembedded row", tags=["plan"])["id"]
        _mark(conn, embedded, age_s=3600)
        _mark(conn, bare, age_s=3600, embedded=False)
        _mark(conn, young, age_s=5, embedded=False)
        assert {m["id"] for m in storage.list_memories(conn, limit=-1)} == set()

        result = storage.import_memories(conn, [{"content": "unrelated import", "tags": ["plan"]}])
        assert result["imported"] == 1
        assert result["sweep"] == {"scanned": 3, "completed": 1, "removed": 1, "pending": 1,
                                   "live_lease": 0, "failed": []}
        # Completed: marker stripped, other metadata kept, visible again.
        assert json.loads(_raw_row(conn, embedded)[1]) == {"k": 1}
        assert storage.get_memory(conn, embedded)["content"] == "stale embedded row"
        assert _raw_row(conn, bare) is None  # removed
        assert _raw_row(conn, young) is not None and storage.get_memory(conn, young) is None  # left pending
        visible = {m["content"] for m in storage.list_memories(conn, limit=-1)}
        assert visible == {"stale embedded row", "unrelated import"}


def test_sweep_handles_a_malformed_marker_as_stale(local_db):
    with storage.connect() as conn:
        mid = _add(conn, "malformed marker row", tags=["plan"])["id"]
        conn.execute("UPDATE memories SET metadata = ? WHERE id = ?",
                     (json.dumps({"import_attempt": "legacy:0"}), mid))
        conn.commit()
        assert storage.sweep_import_markers(conn)["completed"] == 1
        assert _raw_row(conn, mid)[1] is None


def test_reads_hide_a_marked_row_and_repair_never_embeds_it(local_db):
    with storage.connect() as conn:
        keep = _add(conn, "visible neighbour about kiwis", tags=["plan"])["id"]
        mid = _add(conn, "pending row about kiwis", tags=["plan"])["id"]
        _mark(conn, mid, age_s=3600, embedded=False)
        assert storage.get_memory(conn, mid) is None
        assert storage.get_memory(conn, mid, follow="latest") is None
        assert [m["id"] for m in storage.list_memories(conn, limit=-1)] == [keep]
        assert [m["id"] for m in storage.list_memories(conn, query="kiwis")] == [keep]
        assert [m["id"] for m in storage.list_memories(conn, tags_any=["plan"])] == [keep]
        assert [m["id"] for m in storage.list_memories(conn, follow="active")] == [keep]
        assert {r["memory"]["id"] for r in storage.semantic_search(conn, "kiwis")} == {keep}
        assert {r["memory"]["id"] for r in storage.hybrid_search(conn, "kiwis")} == {keep}
        # The corpus snapshot's repair pass skipped it: still no vector.
        assert mid not in _embedded_ids(conn)
        assert storage._hydrate_memories_by_ids(conn, [keep, mid]).keys() == {keep}


def test_import_attempt_is_a_reserved_metadata_key(local_db):
    with storage.connect() as conn:
        with pytest.raises(ValueError, match="reserved"):
            _add(conn, "sneaky", tags=["plan"], metadata={"import_attempt": "x:0:0"})
        # An export taken mid-import carries the marker: import strips it.
        result = storage.import_memories(conn, [{"content": "exported mid import", "tags": ["plan"],
                                                  "metadata": {"import_attempt": "x:0:0", "k": 2}}])
        assert result["imported"] == 1
        [m] = storage.list_memories(conn, limit=-1)
        assert m["metadata"] == {"k": 2}


def test_memory_import_sweep_admin_tool(local_db):
    from memora import server

    bad = asyncio.run(server.memory_import_sweep(older_than_minutes=0))
    assert bad["error"] == "invalid_input"
    with storage.connect() as conn:
        mid = _add(conn, "admin swept row", tags=["plan"])["id"]
        _mark(conn, mid, age_s=3600, embedded=False)
    counts = asyncio.run(server.memory_import_sweep())
    assert counts["removed"] == 1 and counts["failed"] == []


def test_startup_sweep_covers_every_configured_store(tmp_path, monkeypatch):
    from memora import server

    swept = []
    monkeypatch.setattr(storage, "database_registry", lambda: {"a": "x", "b": "y"})
    monkeypatch.setattr(storage, "connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(server, "sweep_import_markers",
                        lambda conn: swept.append(storage.CURRENT_DB.get()))
    server._startup_import_sweep()
    assert swept == ["a", "b"] and storage.CURRENT_DB.get() is None


# --- round 8: import lease, per-row marker time, verified completion; every read site (7100) ---

def test_live_import_rows_survive_a_sweep_past_the_age_bound(fake_d1_backend, monkeypatch):
    """A sweep at +601 s (and at +1700 s) during a live import leaves its rows alone."""
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_upsert = storage._upsert_embedding
        sweeps = []

        def upsert_with_concurrent_sweep(c, mid, vec):
            # Between INSERT and embedding: the row is marked and has no vector yet.
            sweeps.append(storage.sweep_import_markers(conn, now=time.time() + 601))
            sweeps.append(storage.sweep_import_markers(conn, now=time.time() + 1700))
            return real_upsert(c, mid, vec)

        monkeypatch.setattr(storage, "_upsert_embedding", upsert_with_concurrent_sweep)
        result = storage.import_memories(conn, NEW)
        monkeypatch.undo()
        assert result["imported"] == 3 and result["total_errors"] == 0
        assert all(s["removed"] == 0 and s["completed"] == 0 for s in sweeps)
        assert all(s["live_lease"] >= 1 for s in sweeps)
        assert _contents(conn) == ["new row 0", "new row 1", "new row 2"]
        assert set(result["written_ids"]) <= _embedded_ids(conn)
        assert conn.execute("SELECT COUNT(*) FROM import_lease").fetchone()[0] == 0  # released


class _Crash(BaseException):
    pass


def test_rows_of_a_crashed_import_are_swept_once_its_lease_expires(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_upsert, calls = storage._upsert_embedding, {"n": 0}

        def crash_on_second(c, mid, vec):
            calls["n"] += 1
            if calls["n"] == 2:
                raise _Crash()  # the process dies: no cleanup, no lease release
            return real_upsert(c, mid, vec)

        monkeypatch.setattr(storage, "_upsert_embedding", crash_on_second)
        # A crash mid-import: row 0 complete, row 1 inserted without its vector.
        # (The lease release in `finally` still runs on an exception; a real
        # death does not, so drop the release for this simulation.)
        monkeypatch.setattr(storage._ImportLease, "release", lambda self: None)
        with pytest.raises(_Crash):
            storage.import_memories(conn, NEW[:2])
        monkeypatch.undo()
        assert _contents(conn) == ["new row 0", "new row 1"]
        # Lease still live: left alone even past the age bound.
        held = storage.sweep_import_markers(conn, now=time.time() + 601)
        assert held["removed"] == 0 and held["live_lease"] == 1
        # Lease expired: the unembedded row is removed.
        swept = storage.sweep_import_markers(conn, now=time.time() + storage.IMPORT_LEASE_SECONDS + 60)
        assert swept["removed"] == 1 and swept["failed"] == []
        assert _contents(conn) == ["new row 0"]
        assert conn.execute("SELECT COUNT(*) FROM import_lease").fetchone()[0] == 0


def test_rows_marked_by_an_import_without_a_lease_are_stale_by_age(fake_d1_backend):
    with storage.connect() as conn:
        mid = _add(conn, "no lease row", tags=["plan"])["id"]
        _mark(conn, mid, age_s=5, embedded=False, import_id="f" * 32)
        assert storage.sweep_import_markers(conn)["pending"] == 1
        assert storage.sweep_import_markers(conn, now=time.time() + 601)["removed"] == 1


def test_a_row_removed_before_completion_is_never_counted_as_imported(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_upsert = storage._upsert_embedding
        removed = []

        def upsert_then_row_removed(c, mid, vec):
            real_upsert(c, mid, vec)
            c.execute("DELETE FROM memories WHERE id = ?", (mid,))  # e.g. a mistaken concurrent sweep
            removed.append(mid)

        monkeypatch.setattr(storage, "_upsert_embedding", upsert_then_row_removed)
        result = storage.import_memories(conn, [{"content": "vanishing row", "tags": ["plan"]}])
        monkeypatch.undo()
        assert result["imported"] == 0 and result["written_ids"] == [] and result["total_errors"] == 1
        assert "removed before the import completed it" in result["errors"][0]["error"]
        assert not set(removed) & set(result["written_ids"])


def test_a_row_removed_once_is_reinserted_and_counted_once(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_upsert = storage._upsert_embedding
        removed = []

        def remove_first(c, mid, vec):
            real_upsert(c, mid, vec)
            if not removed:
                c.execute("DELETE FROM memories WHERE id = ?", (mid,))
                removed.append(mid)

        monkeypatch.setattr(storage, "_upsert_embedding", remove_first)
        result = storage.import_memories(conn, [{"content": "reinserted row", "tags": ["plan"]}])
        monkeypatch.undo()
        assert result["imported"] == 1 and result["total_errors"] == 0
        [written] = result["written_ids"]
        assert written != removed[0] and _raw_row(conn, written) is not None
        assert _contents(conn) == ["reinserted row"]


def test_no_lease_no_write(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        before = _setup_previous(conn)
        conn.fail_when = lambda sql, params: "import_lease" in sql and sql.lstrip().startswith("INSERT")
        result = storage.import_memories(conn, NEW, strategy="replace")
        conn.fail_when = None
        assert result["replaced"] is False and result["imported"] == 0
        assert result["error"] == "import_lease_unavailable"
        assert _contents(conn) == before  # nothing cleared, nothing written


def test_heartbeat_failure_stops_the_import(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    monkeypatch.setattr(storage, "_IMPORT_HEARTBEAT_S", 0)  # renew at every fence
    with storage.connect() as conn:
        real_upsert, armed = storage._upsert_embedding, {"on": False, "n": 0}

        def upsert(c, mid, vec):
            armed["n"] += 1
            armed["on"] = armed["n"] == 2  # renewals fail from row two's strip on
            return real_upsert(c, mid, vec)

        monkeypatch.setattr(storage, "_upsert_embedding", upsert)
        conn.fail_when = lambda sql, params: armed["on"] and sql.lstrip().startswith("UPDATE import_lease SET lease_until")
        result = storage.import_memories(conn, NEW)
        conn.fail_when = None
        monkeypatch.undo()
        assert result["imported"] == 1 and "lease lost" in result["errors"][0]["error"]
        assert len(result["written_ids"]) == 1 and result["left_marked"][0]["id"] not in result["written_ids"]
        assert _contents(conn) == ["new row 0", "new row 1"]  # row 1 left marked for the sweep; row 2 never written


def test_markers_carry_a_per_row_time(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(storage.time, "time", lambda: clock.__setitem__("t", clock["t"] + 700) or clock["t"])
    markers = []
    real = storage._import_find_marked

    def spy(conn, marker):
        markers.append(marker)
        return real(conn, marker)

    monkeypatch.setattr(storage, "_import_find_marked", spy)
    with storage.connect() as conn:
        storage.import_memories(conn, NEW[:2])
    times = sorted({storage._import_marker_time(m) for m in markers})
    assert len(times) == 2 and times[1] - times[0] >= 700


def _pending_fixture(conn):
    keep = _add(conn, "visible kiwi memory", tags=["plan"], metadata={"section": "A"})["id"]
    mid = _add(conn, "pending kiwi memory", tags=["pendingonly"], metadata={"section": "Hidden"})["id"]
    _mark(conn, mid, age_s=3600, embedded=False)
    return keep, mid


def test_export_skips_a_marked_row(local_db):
    with storage.connect() as conn:
        keep, mid = _pending_fixture(conn)
        assert [e["content"] for e in storage.export_memories(conn)] == ["visible kiwi memory"]


def test_graph_endpoints_skip_a_marked_row(graph_request, local_db):
    with storage.connect() as conn:
        keep, mid = _pending_fixture(conn)
    status, body = graph_request("GET", "/api/memories")
    assert status == 200 and body["total"] == 1 and [m["id"] for m in body["memories"]] == [keep]
    assert "import_attempt" not in json.dumps(body)
    status, graph = graph_request("GET", "/api/graph")
    assert status == 200 and mid not in {n["id"] for n in graph["nodes"]}
    status, _single = graph_request("GET", f"/api/memories/{mid}")
    assert status == 404


def test_every_other_read_site_skips_a_marked_row(local_db):
    with storage.connect() as conn:
        keep, mid = _pending_fixture(conn)
        stats = storage.get_statistics(conn)
        assert stats["total_memories"] == 1 and stats["import_pending"] == 1
        assert "pendingonly" not in storage.collect_all_tags(conn)
        assert ["Hidden"] not in storage.get_hierarchy_paths(conn)
        assert storage.get_memories_metadata_batch(conn, [keep, mid]).keys() == {keep}
        assert storage._memory_exists(conn, mid) is False
        assert storage.boost_memory(conn, mid) is None
        assert storage.update_memory(conn, mid, content="x") is None
        with pytest.raises(ValueError):
            storage.add_link(conn, keep, mid)
        assert all(mid not in (e.get("memory_id"), e.get("id"))
                   for e in storage.find_invalid_tag_entries(conn, ["plan"]))
        conn.execute("INSERT OR REPLACE INTO memories_crossrefs (memory_id, related) VALUES (?, ?)",
                     (keep, json.dumps([{"id": mid, "score": 0.95}])))
        conn.commit()
        pairs = storage.find_duplicate_pairs(conn, 0.5, 100)["pairs"]
        assert all(mid not in (p["memory_a_id"], p["memory_b_id"]) for p in pairs)
        backfill = storage.backfill_tags(conn, dry_run=True)
        assert all(ch.get("id") != mid for ch in backfill.get("changes", []))
        # merge dedupe ignores a pending row: its content is imported as a real memory
        assert storage.import_memories(conn, [{"content": "pending kiwi memory", "tags": ["plan"]}],
                                       strategy="merge")["imported"] == 1


def test_merge_dedupe_ignores_a_live_pending_row(local_db):
    with storage.connect() as conn:
        mid = _add(conn, "young pending text", tags=["plan"])["id"]
        _mark(conn, mid, age_s=5, embedded=False)  # young: the import's own sweep leaves it
        result = storage.import_memories(conn, [{"content": "young pending text", "tags": ["plan"]}],
                                         strategy="merge")
        assert result["imported"] == 1 and result["skipped"] == 0


def test_a_strip_that_does_not_apply_is_never_counted(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_execute = conn.execute

        def execute(sql, params=None):
            if sql.lstrip().startswith("UPDATE memories SET metadata = ? WHERE id = ? AND json_extract"):
                return real_execute("SELECT 1 WHERE 0")  # "succeeds", changes nothing
            return real_execute(sql, params)

        monkeypatch.setattr(conn, "execute", execute)
        result = storage.import_memories(conn, [{"content": "never stripped", "tags": ["plan"]}])
        monkeypatch.undo()
        assert result["imported"] == 0 and result["written_ids"] == []
        assert "marker strip did not apply" in result["errors"][0]["error"]


# --- round 9: one lease per store, fenced renewal and ownership (7106) ---

NEW_B = [{"content": f"b row {i}", "tags": ["plan"]} for i in range(2)]


def test_a_second_import_on_the_same_store_is_refused_while_the_first_runs(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        _setup_previous(conn)
        real_upsert, during = storage._upsert_embedding, {}

        def upsert(c, mid, vec):
            if "b" not in during:  # A is mid-row: B tries a replace of its own
                during["b"] = storage.import_memories(conn, NEW_B, strategy="replace")
                during["b_append"] = storage.import_memories(conn, NEW_B, strategy="append")
            return real_upsert(c, mid, vec)

        monkeypatch.setattr(storage, "_upsert_embedding", upsert)
        a = storage.import_memories(conn, NEW, strategy="replace")
        monkeypatch.setattr(storage, "_upsert_embedding", real_upsert)
        for refused in (during["b"], during["b_append"]):
            assert refused["error"] == "import_in_progress" and refused["imported"] == 0
            assert "nothing was written" in refused["message"]
        assert during["b"]["replaced"] is False
        assert a["replaced"] is True and a["total_errors"] == 0
        assert _contents(conn) == ["new row 0", "new row 1", "new row 2"]  # not mixed
        assert set(a["written_ids"]) == {m["id"] for m in storage.list_memories(conn, limit=-1)}
        # After A released the lease, B runs.
        b = storage.import_memories(conn, NEW_B, strategy="replace")
        assert b["replaced"] is True, b
        assert _contents(conn) == ["b row 0", "b row 1"]


def test_renewal_of_an_expired_lease_is_refused(fake_d1_backend):
    with storage.connect() as conn:
        lease = storage._ImportLease(conn, "a" * 32)
        lease.acquire()
        conn.execute("UPDATE import_lease SET lease_until = '2000-01-01 00:00:00'")
        with pytest.raises(storage.ImportLeaseLostError):
            lease.renew()
        assert conn.execute("SELECT lease_until FROM import_lease").fetchone()[0] == "2000-01-01 00:00:00"
        with pytest.raises(storage.ImportLeaseLostError):
            lease.fence()
        # An expired lease can be taken over by the next import.
        other = storage._ImportLease(conn, "b" * 32)
        other.acquire()
        with pytest.raises(storage.ImportLeaseLostError):
            lease.renew()  # and the old owner can never take it back
        assert conn.execute("SELECT owner FROM import_lease").fetchone()[0] == "b" * 32


def test_a_paused_importer_whose_lease_expired_is_refused_on_resume(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_upsert, calls = storage._upsert_embedding, {"n": 0}

        def pause_on_second_row(c, mid, vec):
            calls["n"] += 1
            if calls["n"] == 2:
                # The importer stalls past its lease; the sweep acts meanwhile.
                conn.execute("UPDATE import_lease SET lease_until = '2000-01-01 00:00:00'")
                swept = storage.sweep_import_markers(conn, now=time.time() + 601)
                assert swept["removed"] == 1  # row 1: marked, not yet embedded
            return real_upsert(c, mid, vec)

        monkeypatch.setattr(storage, "_upsert_embedding", pause_on_second_row)
        result = storage.import_memories(conn, NEW)
        monkeypatch.undo()
        assert result["imported"] == 1 and len(result["written_ids"]) == 1
        assert "lease lost" in result["errors"][0]["error"] and "wrote nothing further" in result["message"]
        assert _contents(conn) == ["new row 0"]  # row 1 swept, row 2 never written
        assert conn.execute("SELECT COUNT(*) FROM import_lease").fetchone()[0] == 0  # not resurrected


def test_ownership_is_checked_before_each_clear_stage(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        before = _setup_previous(conn)
        embedded_before = _embedded_ids(conn)
        real_execute = conn.execute

        def execute(sql, params=None):
            cur = real_execute(sql, params)
            if sql.strip() == "DELETE FROM memories_crossrefs":
                real_execute("UPDATE import_lease SET owner = 'intruder'")  # the lease is taken
            return cur

        monkeypatch.setattr(conn, "execute", execute)
        result = storage.import_memories(conn, NEW, strategy="replace")
        monkeypatch.undo()
        assert result["replaced"] == "partial" and result["clear_stage"] == "embeddings"
        assert "lease lost" in result["errors"][0]["error"] and result["written_ids"] == []
        assert _contents(conn) == before and _embedded_ids(conn) == embedded_before  # nothing further cleared


def test_a_row_completed_just_before_the_lease_is_lost_is_reported_apart(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_execute = conn.execute

        def execute(sql, params=None):
            cur = real_execute(sql, params)
            if sql.lstrip().startswith("SELECT metadata FROM memories WHERE id = ?"):  # the strip read-back
                real_execute("UPDATE import_lease SET owner = 'intruder'")
            return cur

        monkeypatch.setattr(conn, "execute", execute)
        result = storage.import_memories(conn, NEW[:1])
        monkeypatch.undo()
        assert result["imported"] == 0 and result["written_ids"] == []
        [done] = result["unconfirmed_ids"]
        assert storage.get_memory(conn, done)["content"] == "new row 0"
        assert "unconfirmed_ids" in result["message"]


def test_ownership_is_checked_before_each_row_insert(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_marker, calls = storage._import_marker, {"n": 0}

        def marker(import_id, started, index):
            calls["n"] += 1
            if calls["n"] == 2:  # between row 0 (counted) and row 1's INSERT
                conn.execute("UPDATE import_lease SET owner = 'intruder'")
            return real_marker(import_id, started, index)

        monkeypatch.setattr(storage, "_import_marker", marker)
        result = storage.import_memories(conn, NEW)
        monkeypatch.setattr(storage, "_import_marker", real_marker)
        assert result["imported"] == 1 and "lease lost" in result["errors"][0]["error"]
        assert "left_marked" not in result
        assert _contents(conn) == ["new row 0"]  # row 1 was never inserted


# --- round 10: post-write steps only under verified ownership (7116) ---

def _crossref_rows(conn):
    return conn.execute("SELECT COUNT(*) FROM memories_crossrefs").fetchone()[0]


def test_no_post_write_by_an_importer_that_lost_its_lease(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_marker, real_execute, state = storage._import_marker, conn.execute, {"n": 0, "lost": False}
        after_loss = []

        def marker(import_id, started, index):
            state["n"] += 1
            if state["n"] == 2:  # row 0 counted; a takeover importer now owns the store
                real_execute("UPDATE import_lease SET owner = 'takeover'")
                state["lost"] = True
            return real_marker(import_id, started, index)

        def execute(sql, params=None):
            if state["lost"]:
                after_loss.append(sql.strip())
            return real_execute(sql, params)

        monkeypatch.setattr(storage, "_import_marker", marker)
        monkeypatch.setattr(conn, "execute", execute)
        result = storage.import_memories(conn, NEW)
        monkeypatch.setattr(conn, "execute", real_execute)
        monkeypatch.setattr(storage, "_import_marker", real_marker)
        assert result["imported"] == 1 and result["post_write"] == "skipped"
        assert "memory_rebuild_crossrefs" in result["post_write_note"]
        writes = [q for q in after_loss if q.split()[0].upper() in ("INSERT", "UPDATE", "DELETE")]
        assert not [q for q in writes if "memories_crossrefs" in q or "memories_embeddings" in q
                    or "memories_meta" in q], writes
        assert _crossref_rows(conn) == 0


def test_lease_expiring_during_the_rebuild_stops_it_incomplete(fake_d1_backend, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        real_iter = storage._iter_memories_with_embeddings

        def iter_then_expire(c, *a, **k):
            c.execute("UPDATE import_lease SET lease_until = '2000-01-01 00:00:00'")  # rebuild starts, lease lapses
            yield from real_iter(c, *a, **k)

        monkeypatch.setattr(storage, "_iter_memories_with_embeddings", iter_then_expire)
        result = storage.import_memories(conn, NEW)
        monkeypatch.setattr(storage, "_iter_memories_with_embeddings", real_iter)
        assert result["imported"] == 3 and result["total_errors"] == 0
        assert result["post_write"] == "incomplete" and "lost the store's lease" in result["post_write_note"]
        assert _crossref_rows(conn) == 0  # the bulk write was fenced off


def test_a_normal_d1_import_rebuilds_and_restores_the_stamp_under_its_lease(fake_d1_backend, monkeypatch):
    from memora.embeddings import (get_embedding_integrity, invalidate_embedding_integrity_cache,
                                   verify_embedding_integrity)

    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        _setup_previous(conn)
        verify_embedding_integrity(conn, stamp=True)  # the explicit audit writes a stamp (E1: a search no longer does)
        invalidate_embedding_integrity_cache(conn)
        stamp = get_embedding_integrity(conn)
        fences = {"n": 0}
        real_fence = storage._ImportLease.fence

        def counting_fence(self):
            fences["n"] += 1
            return real_fence(self)

        monkeypatch.setattr(storage._ImportLease, "fence", counting_fence)
        import memora.embeddings as embeddings_mod
        real_write, restored = embeddings_mod._write_embedding_integrity, []

        def spy_write(c, value, **kw):
            restored.append((value, fences["n"]))
            return real_write(c, value, **kw)

        monkeypatch.setattr(embeddings_mod, "_write_embedding_integrity", spy_write)
        result = storage.import_memories(conn, NEW, strategy="replace")
        monkeypatch.setattr(embeddings_mod, "_write_embedding_integrity", real_write)
        assert [v for v, _n in restored] == [stamp]  # restored once, under a fence
        assert restored[0][1] > 3 * 3
        assert result["replaced"] is True and result["post_write"] == "done" and "post_write_note" not in result
        assert _crossref_rows(conn) == 3
        invalidate_embedding_integrity_cache(conn)
        assert stamp and get_embedding_integrity(conn) == stamp
        assert fences["n"] > 3 * 3  # rows, plus the post-write steps


# --- typed tags are not project evidence (leader msg 7182; live items 537, 545, 633-668, 961-964) ---

LEGACY_CLMUX_ISSUE = "**clmux: workspace rename does not work**  Attempting to rename a workspace does not take."


def _legacy_issue(conn, tags=("memora/issues",), metadata=None):
    """A clmux issue filed under the old default typed tag memora/issues."""
    meta = {"type": "issue", "status": "open", **(metadata or {})}
    policy = memora.TAG_WHITELIST
    memora.TAG_WHITELIST = set()  # the legacy write path applied it itself
    try:
        return _add(conn, LEGACY_CLMUX_ISSUE, tags=list(tags), metadata=meta)
    finally:
        memora.TAG_WHITELIST = policy


def test_a_typed_tag_is_not_project_evidence(db, projects):
    projects(["memora", "clmux", "acebar", "pi"])
    for kind in ("issues", "todos", "sections", "documents", "knowledge"):
        assert storage._resolve_project(None, [f"memora/{kind}"], {"type": "issue"}) is None
    # A non-typed project tag is evidence, and a typed tag never makes it ambiguous.
    assert storage._resolve_project(None, ["memora/issues", "clmux/tui"], {"type": "issue"}) == "clmux"
    assert storage._resolve_project(None, ["memora/issues"], {"project": "clmux"}) == "clmux"
    assert storage._resolve_project("clmux", ["memora/issues"], {}) == "clmux"


def test_a_legacy_typed_issue_no_longer_resolves_to_memora(db, projects):
    projects(["memora", "clmux", "acebar", "pi"])
    with storage.connect() as conn:
        mid = _legacy_issue(conn)["id"]
        stored = storage.get_memory(conn, mid)
    assert (stored["metadata"] or {}).get("section") is None  # no memora section from the typed tag
    assert stored["tags"] == ["memora/issues"]  # left as is while no project is resolved


def test_the_typed_tag_follows_a_later_resolved_project(default_policy_db, projects):
    projects(["memora", "clmux", "acebar", "pi"])
    with storage.connect() as conn:
        mid = _legacy_issue(conn)["id"]
        # Declaring the project re-prefixes the memory's own typed tag, under
        # the default tag policy (exempt: memora applied it).
        updated = storage.update_memory(conn, mid, metadata={"project": "clmux"})
    assert updated["tags"] == ["clmux/issues"]
    assert updated["metadata"]["project"] == "clmux"


def test_the_typed_tag_follows_a_non_typed_project_tag_on_a_tag_update(db, projects):
    projects(["memora", "clmux", "acebar", "pi"])
    with storage.connect() as conn:
        mid = _legacy_issue(conn)["id"]
        updated = storage.update_memory(conn, mid, tags=["memora/issues", "clmux/tui"])
    assert sorted(updated["tags"]) == ["clmux/issues", "clmux/tui"]


def test_an_update_without_a_project_keeps_the_legacy_typed_tag(default_policy_db, projects):
    projects(["memora", "clmux", "acebar", "pi"])
    with storage.connect() as conn:
        mid = _legacy_issue(conn)["id"]
        updated = storage.update_memory(conn, mid, content=LEGACY_CLMUX_ISSUE + " Still open.")
    assert updated["tags"] == ["memora/issues"]


def test_another_kinds_typed_tag_is_not_exempt(default_policy_db, projects):
    projects(["memora", "clmux"])
    with storage.connect() as conn:
        mid = _legacy_issue(conn)["id"]
        # "todos" is not this issue's own kind: a user tag, enforced by the policy.
        with pytest.raises(ValueError):
            storage.update_memory(conn, mid, tags=["memora/issues", "pi/todos"])


def test_export_import_round_trip_of_a_legacy_typed_issue(db, projects):
    projects(["memora", "clmux", "acebar", "pi"])
    with storage.connect() as conn:
        _legacy_issue(conn)
        exported = storage.export_memories(conn)
    (entry,) = exported
    assert entry["system_tags"] == ["memora/issues"]
    with storage.connect() as conn:
        # Restored as is while no project is resolved...
        assert storage.import_memories(conn, [dict(entry)], strategy="replace")["replaced"] is True
        (restored,) = storage.list_memories(conn, limit=-1)
        assert restored["tags"] == ["memora/issues"]
        # ...and re-prefixed when the import declares the project.
        assert storage.import_memories(conn, [dict(entry, project="clmux")], strategy="replace")["replaced"] is True
        (restored,) = storage.list_memories(conn, limit=-1)
        assert restored["tags"] == ["clmux/issues"] and restored["metadata"]["project"] == "clmux"


@pytest.mark.parametrize("strategy", ["append", "replace"])
def test_an_import_with_a_forged_typed_tag_prefix_is_refused(db, projects, strategy):
    """Only the OLD DEFAULT memora/<kind> is a legacy exception; any other
    prefix on a no-project memory is a forged system tag."""
    projects(["memora", "clmux", "acebar", "pi"])
    with storage.connect() as conn:
        _legacy_issue(conn)  # the store already holds one memory
        before = [m["id"] for m in storage.list_memories(conn, limit=-1)]
        forged = {"content": "forged typed tag", "tags": ["evil/issues"], "system_tags": ["evil/issues"],
                  "metadata": {"type": "issue"}}
        result = storage.import_memories(conn, [forged], strategy=strategy)
        assert result["imported"] == 0 and result["total_errors"] == 1
        assert "invalid system tag" in result["errors"][0]["error"]
        if strategy == "replace":
            assert result["replaced"] is False
        assert [m["id"] for m in storage.list_memories(conn, limit=-1)] == before  # nothing written
        # The old default itself is still accepted.
        ok = dict(forged, content="old default typed tag", tags=["memora/issues"], system_tags=["memora/issues"])
        assert storage.import_memories(conn, [ok])["imported"] == 1
