"""Item 242: the dream pass (memora/dream.py)."""
import contextlib
import hashlib
import json
import os
import subprocess

import pytest

import memora.storage as storage
from memora import dream
from memora.dream import Claim, Config, DreamError, Evidence, Limits, Mem

pytestmark = pytest.mark.usefixtures("local_db")


# ----------------------------------------------------------------------------- helpers


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def gitrepo(tmp_path):
    """A source repo plus the mirror clone the pass reads. Returns (cfg_repos, git_dir, sha, src)."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "dev")
    (src / "memora").mkdir()
    (src / "memora" / "dream.py").write_text("x = 1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "feat(item242): dream pass")
    sha = _git(src, "rev-parse", "HEAD")
    _git(src, "tag", "v0.5.7.7")
    git_dir = tmp_path / "mirrors"
    git_dir.mkdir()
    _git(tmp_path, "clone", "-q", "--mirror", str(src), str(git_dir / "proj.git"))
    return {"proj": {"dir": "proj.git", "ref": "dev", "projects": ["proj"]}}, git_dir, sha, src


def _cfg(tmp_path, repos=None, git_dir=None, **limits):
    return Config(limits=Limits(**limits), git_dir=git_dir, repos=repos or {}, state_dir=tmp_path / "state",
                  corrections=True)


def _add(content, tags=None, metadata=None):
    with storage.connect() as conn:
        r = storage.add_memory(conn, content=content, tags=[*(tags or []), "proj"], metadata=metadata or {})   # "proj" names the mirror
        conn.commit()
    return r["id"] if "id" in r else r["memory"]["id"]


def _state():
    """Everything a write could change: rows and crossrefs, hashed."""
    with storage.connect() as conn:
        rows = conn.execute("SELECT id, content, metadata, tags, updated_at FROM memories ORDER BY id").fetchall()
        refs = conn.execute("SELECT memory_id, related FROM memories_crossrefs ORDER BY memory_id").fetchall()
    return hashlib.sha256(json.dumps([[tuple(r) for r in rows], [tuple(r) for r in refs]], default=str).encode()).hexdigest()


def _dry(cfg, judge, **kw):
    with contextlib.closing(storage.connect_without_schema()) as conn:
        return dream.run_dry(conn, cfg, judge, **kw)


def _answer(claims_verdicts, correction=None):
    def judge(prompt, timeout=None):
        ids = [l.split("id=")[1].split()[0] for l in prompt.splitlines() if l.startswith("- id=")]
        out = []
        for cid in ids:
            v, quote = claims_verdicts(cid, prompt)
            out.append({"id": cid, "verdict": v, "evidence": quote})
        corr = correction
        if corr == "AUTO":          # a full rewrite of the note the judge was shown (Item 244 accepts those)
            corr = prompt.split("<<<\n", 1)[1].split("\n>>>", 1)[0] + " (corrected)"
        return json.dumps({"claims": out, "correction": corr})
    return judge


def _ev_line(cid, prompt):
    return prompt.split(f"id={cid} ")[1].split("evidence: ")[1].splitlines()[0]


def _quote_first_evidence(cid, prompt):
    # a quote copied from this claim's evidence line
    return _ev_line(cid, prompt)[:40]


# ----------------------------------------------------------------------------- claims


def test_extract_claims_kinds_and_noise():
    text = ("Landed in 442ca1b4ef49 as v0.5.7.6; Item 141 fixed memora/storage.py; see mail #16307. "
            "Count is 1234567 and the word defaced is not a sha.")
    got = {(c.kind, c.value) for c in dream.extract_claims(text)}
    assert ("sha", "442ca1b4ef49") in got and ("tag", "v0.5.7.6") in got and ("item", "141") in got
    assert ("path", "memora/storage.py") in got and ("msg", "16307") in got
    assert not any(v in ("1234567", "defaced") for _k, v in got)
    assert dream.extract_claims("nothing checkable here") == []


def test_extract_claims_is_capped():
    text = " ".join(f"Item {n}" for n in range(1, 40))
    assert len(dream.extract_claims(text, limit=5)) == 5


# ----------------------------------------------------------------------------- fail closed


def _claim_and_ev(text="commit abc1234 exists on dev"):
    return [Claim("c1", "sha", "abc1234", "abc1234")], {"c1": Evidence(True, text)}


def test_valid_answer_is_confirmed_control():
    claims, ev = _claim_and_ev()
    out, corr, prob = dream.parse_verdicts(
        json.dumps({"claims": [{"id": "c1", "verdict": "confirmed", "evidence": "exists on dev"}], "correction": None}), claims, ev, "n")
    assert out["c1"]["verdict"] == "confirmed" and prob is None


@pytest.mark.parametrize("answer", [
    None, "", "   ", "not json", "[]", "{}", '{"claims": "x"}', '{"claims": [1, 2]}',
    json.dumps({"claims": [{"id": "c9", "verdict": "confirmed", "evidence": "exists on dev"}]}),     # unknown id
    json.dumps({"claims": [{"id": "c1", "verdict": "probably", "evidence": "exists on dev"}]}),      # bad verdict
    json.dumps({"claims": [{"id": "c1", "verdict": "confirmed", "evidence": "invented quote here"}]}),  # not in source
    json.dumps({"claims": [{"id": "c1", "verdict": "confirmed"}]}),                                   # no quote
    json.dumps({"claims": [{"id": "c1", "verdict": "confirmed", "evidence": "exists on dev"},
                           {"id": "c1", "verdict": "wrong", "evidence": "exists on dev"}]}),         # duplicate
])
def test_bad_answers_are_unverifiable_never_confirmed(answer):
    claims, ev = _claim_and_ev()
    out, _corr, _prob = dream.parse_verdicts(answer, claims, ev, "n")
    assert out["c1"]["verdict"] == "unverifiable"


def test_confirmed_without_source_evidence_is_unverifiable():
    claims = [Claim("c1", "sha", "abc1234", "abc1234")]
    ev = {"c1": Evidence(False, "no clone")}
    out, *_ = dream.parse_verdicts(
        json.dumps({"claims": [{"id": "c1", "verdict": "confirmed", "evidence": "no clone"}]}), claims, ev, "n")
    assert out["c1"]["verdict"] == "unverifiable"


def test_correction_needs_a_stale_or_wrong_claim():
    claims, ev = _claim_and_ev()
    ans = {"claims": [{"id": "c1", "verdict": "confirmed", "evidence": "exists on dev"}], "correction": "new text"}
    _out, corr, prob = dream.parse_verdicts(json.dumps(ans), claims, ev, "old")
    assert corr is None and prob is None and _out["c1"]["verdict"] == "confirmed"
    ans["claims"][0]["verdict"] = "stale"
    _out, corr, prob = dream.parse_verdicts(json.dumps(ans), claims, ev, "old")
    assert corr == "new text"


def test_judge_exception_and_missing_judge_fail_closed(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    _add(f"Item 242 landed as {sha[:12]}", metadata={"project": None})
    cfg = _cfg(tmp_path, repos, git_dir)

    def boom(_p, timeout=None):
        raise RuntimeError("provider down")

    for judge in (boom, None, lambda _p, timeout=None: "garbage"):
        r = _dry(cfg, judge)
        f = r["findings"][0]
        assert {c["verdict"] for c in f["claims"]} == {"unverifiable"}
        assert f["proposals"] == [] or f["proposals"][0]["action"] != "supersede"
        assert r["counts"]["claims"]["confirmed"] == 0
        # not judged -> never stamped, so the memory is retried next run
        assert f["problem"] and not any(p["action"] == "annotate" for p in f["proposals"])


def test_openai_judge_refuses_without_model_or_key(monkeypatch):
    monkeypatch.delenv("MEMORA_DREAM_MODEL", raising=False)
    with pytest.raises(dream.JudgeError):
        dream.OpenAIJudge()
    monkeypatch.setenv("MEMORA_DREAM_MODEL", "m")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(dream.JudgeError):
        dream.OpenAIJudge()


def test_cli_without_judge_is_exit_3_and_all_unverifiable(tmp_path, monkeypatch, gitrepo, capsys):
    repos, git_dir, sha, _src = gitrepo
    _add(f"Landed {sha[:12]}")
    monkeypatch.delenv("MEMORA_DREAM_MODEL", raising=False)
    cfgp = tmp_path / "dream.json"
    cfgp.write_text(json.dumps({"git_dir": str(git_dir), "repos": repos, "state_dir": str(tmp_path / "st")}))
    out = tmp_path / "report.json"
    assert dream.main(["--config", str(cfgp), "--out", str(out)]) == 3
    rep = json.loads(out.read_text())
    assert rep["counts"]["claims"]["confirmed"] == 0 and rep["judge"]["status"].startswith("unavailable")
    assert oct(out.stat().st_mode & 0o777) == "0o600"


# ----------------------------------------------------------------------------- end to end


def test_dry_run_confirms_stale_and_writes_nothing(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    good = _add(f"Item 242 landed as {sha[:12]}", metadata={"project": None})
    bad = _add("The helper lives in memora/missing_file.py in this repo. The release v9.9.9 is tagged in this repo", metadata={"project": None})
    cfg = _cfg(tmp_path, repos, git_dir)
    before = _state()

    def verdicts(cid, prompt):
        line = _ev_line(cid, prompt)
        missing = ("none" in line) or ("not found" in line) or ("does not exist" in line)
        return ("wrong" if missing else "confirmed", line[:40])

    judge = _answer(verdicts, correction="The helper lives in memora/dream.py in this repo. The release v0.5.7.7 is tagged in this repo")
    # claims for `bad` carry 'none'/'does not exist' evidence; the judge quotes them
    r = _dry(cfg, judge)
    assert _state() == before                                    # dry run: no write at all
    by_id = {f["memory_id"]: f for f in r["findings"]}
    assert by_id[good]["overall"] == "all_found"
    assert by_id[good]["proposals"][0]["metadata"]["references_checked"]["how"] == "git+llm"
    assert by_id[bad]["overall"] == "missing"
    assert any(p["action"] == "supersede" for p in by_id[bad]["proposals"])
    assert r["git"]["proj"] == "fetched"
    assert r["counts"]["claims"]["confirmed"] >= 1 and r["counts"]["claims"]["wrong"] >= 1

    # apply: only additions
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(r, default=str))
    plan = dream.load_plan(str(p))
    with storage.connect() as conn:
        n_before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        content_before = {i: c for i, c in conn.execute("SELECT id, content FROM memories")}
    out = dream.apply_plan(plan, Limits(max_writes=50))
    assert out["writes"] >= 3 and not out["skipped"], out
    with storage.connect() as conn:
        n_after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        content_after = {i: c for i, c in conn.execute("SELECT id, content FROM memories")}
        refs = {i: json.loads(r_ or "[]") for i, r_ in conn.execute("SELECT memory_id, related FROM memories_crossrefs")}
        meta_good = json.loads(conn.execute("SELECT metadata FROM memories WHERE id=?", (good,)).fetchone()[0])
    assert n_after == n_before + 1                                # one corrected memory added, none deleted
    assert all(content_after[i] == content_before[i] for i in content_before)   # no in-place content edit
    assert "references_checked" in meta_good and "verified" not in meta_good
    new_id = [a["new_id"] for a in out["applied"] if a.get("new_id")][0]
    assert any(x.get("edge_type") == "supersedes" and x["id"] == bad for x in refs[new_id])
    assert any(x.get("edge_type") == "superseded_by" and x["id"] == new_id for x in refs[bad])


def test_item_record_is_never_superseded_only_linked_related(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    rec = _add(f"Item 7 landed as {sha[:12]}. The release v9.9.9 is tagged in this repo", tags=["item-record"], metadata={"kind": "item-record"})
    cfg = _cfg(tmp_path, repos, git_dir)
    judge = _answer(lambda cid, p: ("wrong", _quote_first_evidence(cid, p)), correction="AUTO")
    r = _dry(cfg, judge)
    acts = [p["action"] for p in r["findings"][0]["proposals"]]
    assert "supersede" in acts          # the plan only proposes; protection is read at write time
    out = dream.apply_plan(r, Limits(max_writes=10))
    assert [a["edge"] for a in out["applied"] if "edge" in a] == ["related_to"]
    with storage.connect() as conn:
        refs = json.loads(conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id=?", (rec,)).fetchone()[0])
    assert not any(x.get("edge_type") == "superseded_by" for x in refs)


def test_apply_refuses_edited_plan_and_changed_memory(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid = _add(f"landed {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir), _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    path = tmp_path / "p.json"
    r["findings"][0]["proposals"][0]["metadata"]["references_checked"]["how"] = "tampered"
    path.write_text(json.dumps(r, default=str))
    with pytest.raises(DreamError, match="digest"):
        dream.load_plan(str(path))
    r = _dry(_cfg(tmp_path, repos, git_dir), _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    with storage.connect() as conn:
        storage.update_memory(conn, mid, content=f"changed meanwhile {sha[:12]}")
    out = dream.apply_plan(r, Limits(max_writes=10))
    assert out["writes"] == 0 and "changed" in out["skipped"][0]["why"]


def test_old_plan_is_refused(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    _add(f"landed {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir), None, now=1000.0)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(r, default=str))
    with pytest.raises(DreamError, match="old"):
        dream.load_plan(str(path))


# ----------------------------------------------------------------------------- caps


def test_cap_max_memories(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    for i in range(5):
        _add(f"note {i} landed as {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=2), None)
    assert r["counts"]["checked"] == 2 and r["counts"]["skipped"]["over_cap"] == 3 and r["stopped_by"] == "max_memories"


def test_cap_max_llm_calls(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    for i in range(5):
        _add(f"note {i} landed as {sha[:12]}")
    calls = []

    def judge(p, timeout=None):
        calls.append(p)
        return "{}"

    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10, max_llm_calls=2), judge)
    assert len(calls) == 2 and r["counts"]["llm_calls"] == 2 and r["stopped_by"] == "max_llm_calls"
    assert r["counts"]["checked"] == 2


def test_cap_max_seconds(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    for i in range(5):
        _add(f"note {i} landed as {sha[:12]}")
    t = iter(range(0, 10000, 100))                     # every clock read advances 100 s
    calls = []
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10, max_seconds=250),
             lambda p, timeout=None: calls.append(p) or "{}", clock=lambda: float(next(t)))
    assert r["stopped_by"] == "max_seconds" and r["counts"]["checked"] < 5


def test_cap_max_writes_on_apply(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    for i in range(5):
        _add(f"note {i} landed as {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
             _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    out = dream.apply_plan(r, Limits(max_writes=2))
    assert out["writes"] == 2 and len(out["applied"]) == 2 and len(out["deferred"]) == 3


def test_cap_clone_size_checked_before_fetch(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    _add(f"landed {sha[:12]}")
    cfg = _cfg(tmp_path, repos, git_dir, max_clone_mb=0)       # any clone is over the cap
    ran = []
    real = subprocess.run
    src = dream.GitSource(cfg, runner=lambda cmd, **kw: ran.append(cmd) or real(cmd, **kw))
    r = _dry(cfg, None, source=src)
    assert "not fetched" in r["git"]["proj"]
    assert not any("fetch" in c for c in ran)                  # never even tried
    cfg2 = _cfg(tmp_path, repos, git_dir)                       # control: under the cap it does fetch
    ran.clear()
    dream.GitSource(cfg2, runner=lambda cmd, **kw: ran.append(cmd) or real(cmd, **kw)).prepare()
    assert any("fetch" in c for c in ran)


def test_git_allows_plumbing_only(tmp_path, gitrepo):
    repos, git_dir, _sha, _src = gitrepo
    src = dream.GitSource(_cfg(tmp_path, repos, git_dir))
    for bad in ("checkout", "clone", "commit", "push", "gc", "config", "worktree", "submodule", "merge"):
        with pytest.raises(DreamError):
            src.git("proj", bad)


# ----------------------------------------------------------------------------- lock, nice


def test_one_run_at_a_time(tmp_path):
    with dream.run_lock(tmp_path / "s"):
        with pytest.raises(DreamError, match="another dream run"):
            with dream.run_lock(tmp_path / "s"):
                pass
    with dream.run_lock(tmp_path / "s"):          # released afterwards
        pass


def test_lock_is_taken_by_the_cli(tmp_path):
    cfgp = tmp_path / "c.json"
    cfgp.write_text(json.dumps({"state_dir": str(tmp_path / "s")}))
    with dream.run_lock(tmp_path / "s"):
        assert dream.main(["--config", str(cfgp)]) == 2


def test_lower_priority_calls_nice_and_ionice(monkeypatch):
    seen = {}
    monkeypatch.setattr(os, "nice", lambda n: seen.setdefault("nice", n) or n)
    monkeypatch.setattr(dream.subprocess, "run", lambda cmd, **kw: seen.setdefault("ionice", cmd) and
                        subprocess.CompletedProcess(cmd, 0))
    out = dream.lower_priority(12)
    assert seen["nice"] == 12 and seen["ionice"][:3] == ["ionice", "-c", "3"] and out["ionice"] is True


# ----------------------------------------------------------------------------- selection, scans, CLI


def test_priority_volatile_and_item_records_first_and_recent_checks_skipped():
    now = 2_000_000_000.0
    mems = [
        Mem(1, "A note about v0.5.7.6 on branch dev", {}, [], None, None, []),
        Mem(2, "Item 5 plain session note about memora/x.py", {}, [], None, None, []),
        Mem(3, "Item 6 record memora/y.py", {"kind": "item-record"}, ["item-record"], None, None, []),
        Mem(4, "Item 7 checked memora/z.py", {"references_checked": {"at": dream._iso(now - 86400)}}, [], None, None, []),
        Mem(5, "no claims", {}, [], None, None, []),
        Mem(6, "Item 9 old memora/q.py", {}, [], None, None, [{"edge_type": "superseded_by", "id": 1}]),
    ]
    gone = dream.superseded_ids(mems)
    picked, skipped = dream.select(mems, gone, {2: 0}, Limits(max_memories=10), now)
    ids = [m.id for m, _ in picked]
    assert 4 not in ids and 5 not in ids and 6 not in ids
    assert skipped["checked_recently"] == 1 and skipped["no_checkable_claim"] == 1 and skipped["superseded"] == 1
    assert ids.index(1) < ids.index(2) and ids.index(3) < ids.index(2)


def test_read_frequency_raises_priority(tmp_path):
    led = tmp_path / "led.jsonl"
    led.write_text("\n".join(json.dumps({"memories": [{"id": 8}]}) for _ in range(30)) + "\nnot json\n")
    mems = [Mem(7, "Item 5 note memora/a.py", {}, [], None, None, []), Mem(8, "Item 5 note memora/b.py", {}, [], None, None, [])]
    picked, _ = dream.select(mems, set(), dream.read_counts(led), Limits(max_memories=1), 1e9)
    assert picked[0][0].id == 8


def test_dup_and_dangling_scans_and_apply_only_adds(tmp_path):
    long = "The nightly compare job runs at three in the morning on the build host and mails the result to the leader"
    a, b = _add(long), _add(long + " today")
    c = _add("Item 3 unrelated note about something else entirely, long enough to count here ok")
    with storage.connect() as conn:                              # c gets a link to a memory that does not exist
        conn.execute("INSERT OR REPLACE INTO memories_crossrefs (memory_id, related) VALUES (?, ?)",
                     (c, json.dumps([{"id": 9999, "edge_type": "references"}])))
        conn.commit()
    r = _dry(_cfg(tmp_path), None)
    assert [(d["older"], d["newer"]) for d in r["duplicates"]] == [(a, b)]
    assert r["dangling"][0]["memory_id"] == c and r["dangling"][0]["missing_ids"] == [9999]
    out = dream.apply_plan(r, Limits(max_writes=10))
    with storage.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        refs_c = json.loads(conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id=?", (c,)).fetchone()[0])
    assert n == 3 and any(x["id"] == 9999 for x in refs_c)       # the dangling edge is recorded, never removed
    assert {"duplicate": [a, b], "edge": "related_to"} in out["applied"]
    with storage.connect() as conn:
        refs_a = json.loads(conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id=?", (a,)).fetchone()[0])
    assert not any(x.get("edge_type") == "superseded_by" for x in refs_a)   # nothing is hidden by a lexical match


def test_dry_run_on_readonly_copy_cannot_write(tmp_path):
    _add("Item 4 note memora/a.py")
    dbfile = tmp_path / "memories.db"
    conn = dream.open_readonly_file(str(dbfile))
    with pytest.raises(Exception):
        conn.execute("INSERT INTO memories (content) VALUES ('x')")
    r = dream.run_dry(conn, _cfg(tmp_path), None)
    assert r["counts"]["selected"] == 1


def test_cli_apply_rejects_db_and_missing_plan(tmp_path):
    cfgp = tmp_path / "c.json"
    cfgp.write_text(json.dumps({"state_dir": str(tmp_path / "s")}))
    assert dream.main(["--config", str(cfgp), "--apply"]) == 2
    assert dream.main(["--config", str(cfgp), "--apply", "--plan", "x", "--db", "y"]) == 2
    assert dream.main(["--config", str(tmp_path / "missing.json")]) == 2


def test_unknown_limit_and_bad_history_cmd_refused(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"limits": {"max_everything": 1}}))
    with pytest.raises(DreamError):
        Config.load(str(p))
    p.write_text(json.dumps({"history_cmd": "a b"}))
    with pytest.raises(DreamError):
        Config.load(str(p))


def test_message_claims_use_history_cmd_or_stay_unverifiable(tmp_path, gitrepo):
    repos, git_dir, _sha, _src = gitrepo
    _add("The leader sent mail #16307 about this")
    r = _dry(_cfg(tmp_path, repos, git_dir), lambda p, timeout=None: "{}")
    assert r["findings"][0]["claims"][0]["verdict"] == "unverifiable"
    cfg = _cfg(tmp_path, repos, git_dir)
    cfg.history_cmd = ["echo"]
    r = _dry(cfg, _answer(lambda c, p: ("confirmed", "message 16307")))
    assert r["findings"][0]["claims"][0]["verdict"] == "confirmed"


def test_out_dir_names_a_timestamped_report_0600(tmp_path, monkeypatch, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    _add(f"landed {sha[:12]}")
    monkeypatch.delenv("MEMORA_DREAM_MODEL", raising=False)
    cfgp = tmp_path / "c.json"
    cfgp.write_text(json.dumps({"git_dir": str(git_dir), "repos": repos, "state_dir": str(tmp_path / "s")}))
    dream.main(["--config", str(cfgp), "--out-dir", str(tmp_path / "reports")])
    files = list((tmp_path / "reports").glob("dream-*.json"))
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == "0o600"
    assert dream.load_plan(str(files[0]), now=json.loads(files[0].read_text())["created_ts"])["mode"] == "dry-run"


# ----------------------------------------------------------------------------- round 2: review findings


def _two_claims():
    claims = [Claim("c1", "sha", "abc1234", "abc1234"), Claim("c2", "tag", "v1.0.0", "v1.0.0")]
    ev = {"c1": Evidence(True, "commit abc1234 exists"), "c2": Evidence(True, "tag v1.0.0 exists")}
    return claims, ev


def _row(i, v="confirmed", q="exists"):
    return {"id": i, "verdict": v, "evidence": q}


@pytest.mark.parametrize("answer", [
    {"claims": [_row("c1")], "correction": None},                                # c2 omitted
    {"claims": [_row("c1"), _row("c2"), _row("c3")], "correction": None},    # unknown id next to valid rows
    {"claims": [_row("c1"), _row("c2"), "junk"], "correction": None},                            # a non-object row
    {"claims": [_row("c1"), {**_row("c2"), "extra": 1}], "correction": None},                    # extra key in a row
    {"claims": [_row("c1"), _row("c2")], "correction": None, "other": 1},    # extra top-level key
    {"claims": [_row("c1"), _row("c2")]},                                    # correction key missing
    {"claims": [_row("c1"), _row("c2")], "correction": 5},                   # wrongly typed correction
    {"claims": [_row("c1"), _row([])], "correction": None},                                      # unhashable id (list)
    {"claims": [_row("c1"), _row({})], "correction": None},                                      # unhashable id (dict)
    {"claims": [_row("c1"), _row(7)], "correction": None},                                       # non-string id
    {"claims": [_row("c1"), _row("c2", v=["confirmed"])], "correction": None},                   # unhashable verdict
    {"claims": [_row("c1"), _row("c2", q=5)], "correction": None},                               # non-string quote
    {"claims": [_row("c1"), _row("c1"), _row("c2")], "correction": None},                        # repeated id
])
def test_one_bad_row_makes_every_claim_unverifiable_and_unstamped(answer):
    claims, ev = _two_claims()
    out, corr, prob = dream.parse_verdicts(json.dumps(answer), claims, ev, "n")   # must not raise
    assert prob and {v["verdict"] for v in out.values()} == {"unverifiable"} and corr is None


def test_well_formed_answer_control():
    claims, ev = _two_claims()
    out, _c, prob = dream.parse_verdicts(json.dumps({"claims": [_row("c1"), _row("c2")], "correction": None}), claims, ev, "n")
    assert prob is None and {v["verdict"] for v in out.values()} == {"confirmed"}


def test_overall_needs_every_claim_confirmed():
    assert dream.overall(["confirmed", "confirmed"]) == "all_found"
    assert dream.overall(["confirmed", "unverifiable"]) == "partial"
    assert dream.overall(["unverifiable"]) == "unknown"
    assert dream.overall(["confirmed", "stale", "unverifiable"]) == "stale"
    assert dream.overall(["confirmed", "wrong"]) == "missing"
    assert dream.overall([]) == "unknown"


def test_mixed_coverage_is_unconfirmed_not_verified(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    _add(f"Landed as {sha[:12]} with tag v9.9.9")          # sha exists, the tag does not
    def judge(prompt, timeout=None):
        ids = [l.split("id=")[1].split()[0] for l in prompt.splitlines() if l.startswith("- id=")]
        rows = [{"id": i, "verdict": "confirmed" if "-> " in _ev_line(i, prompt) else "unverifiable",
                 "evidence": _ev_line(i, prompt)[:30] if "-> " in _ev_line(i, prompt) else None} for i in ids]
        return json.dumps({"claims": rows, "correction": None})
    r = _dry(_cfg(tmp_path, repos, git_dir), judge)
    f = r["findings"][0]
    assert f["overall"] == "partial" and r["counts"]["memories"]["partial"] == 1
    meta = f["proposals"][0]["metadata"]
    rc = meta["references_checked"]
    assert "verified" not in meta and rc["summary"] == "partial" and rc["found"] == 1 and rc["unknown"] == 1


def test_judge_omitting_an_id_is_not_stamped(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    _add(f"Landed as {sha[:12]} in Item 242")
    def judge(prompt, timeout=None):
        return json.dumps({"claims": [{"id": "c1", "verdict": "confirmed", "evidence": _ev_line("c1", prompt)[:30]}], "correction": None})
    f = _dry(_cfg(tmp_path, repos, git_dir), judge)["findings"][0]
    assert f["problem"] and not f["proposals"] and {c["verdict"] for c in f["claims"]} == {"unverifiable"}


# --- F3 deadlines and write accounting


def test_every_phase_is_bounded_by_the_remaining_deadline(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    for i in range(3):
        _add(f"note {i} landed as {sha[:12]}")
    seen_git, seen_judge = [], []
    real = subprocess.run
    clock = [0.0]

    def runner(cmd, **kw):
        seen_git.append(kw["timeout"])
        clock[0] += 2.0                                  # every git call takes 2 "seconds"
        return real(cmd, **kw)

    def judge(p, timeout=None):
        seen_judge.append(timeout)
        clock[0] += 3.0
        return "{}"

    cfg = _cfg(tmp_path, repos, git_dir, max_seconds=20, git_timeout=60, max_memories=10)
    src = dream.GitSource(cfg, runner=runner)
    r = _dry(cfg, judge, source=src, clock=lambda: clock[0])
    assert max(seen_git) <= 20 and all(t <= 20 for t in seen_judge)         # never the full per-call timeout
    assert all(b >= a for a, b in zip(seen_git, seen_git[1:])) is False      # budget shrinks as time passes
    assert r["stopped_by"] == "max_seconds"
    assert r["counts"]["scans_skipped"] is True and r["duplicates"] == [] and r["dangling"] == []


def test_a_call_with_no_time_left_is_never_started(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    cfg = _cfg(tmp_path, repos, git_dir)
    ran = []
    src = dream.GitSource(cfg, runner=lambda *a, **k: ran.append(a) or None, remaining=lambda: 0.0)
    assert src.git("proj", "rev-parse", "HEAD").ok is False and not ran


def test_watchdog_fires_and_can_be_cancelled():
    hit = []
    t = dream.start_watchdog(0.0, exit_fn=hit.append, grace=0.05)
    t.join(2)
    assert hit == [4]
    hit.clear()
    dream.start_watchdog(10, exit_fn=hit.append, grace=0).cancel()
    assert hit == []


def test_write_is_charged_before_dispatch_so_a_failing_commit_still_counts(tmp_path, gitrepo, monkeypatch):
    repos, git_dir, sha, _src = gitrepo
    for i in range(5):
        _add(f"note {i} landed as {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
             _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    real, attempts = storage.update_memory, []

    def commits_then_raises(conn, mid, **kw):
        attempts.append(mid)
        real(conn, mid, **kw)
        raise RuntimeError("connection dropped after commit")

    monkeypatch.setattr(storage, "update_memory", commits_then_raises)
    out = dream.apply_plan(r, Limits(max_writes=2))
    assert len(attempts) == 2 and out["writes_charged"] == 2 and len(out["deferred"]) == 3
    assert all("counted as written" in s["why"] for s in out["skipped"])


def test_apply_stops_starting_writes_after_the_deadline(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    for i in range(4):
        _add(f"note {i} landed as {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
             _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    c = [0.0]

    def clk():
        c[0] += 3.0
        return c[0]
    out = dream.apply_plan(r, Limits(max_writes=50, max_seconds=25), clock=clk)
    assert len(out["applied"]) < 4 and (out["aborted"] == "max_seconds" or any(d["why"] == "max_seconds" for d in out["deferred"]))
    assert not any(e.get("dispatched") and e.get("end") is None for e in out["dispatch_log"][:-1])


def test_openai_judge_has_no_sdk_retries(monkeypatch):
    import types, sys as _sys
    made = {}

    class FakeClient:
        def __init__(self, **kw):
            made.update(kw)
    monkeypatch.setitem(_sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeClient))
    monkeypatch.setenv("MEMORA_DREAM_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    dream.OpenAIJudge()
    assert made["max_retries"] == 0


# --- F4 evidence honesty


def test_a_failed_git_command_is_unknown_not_not_found(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    cfg = _cfg(tmp_path, repos, git_dir)
    real = subprocess.run

    def flaky(cmd, **kw):
        if "rev-parse" in cmd and "--verify" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal")
        return real(cmd, **kw)

    src = dream.GitSource(cfg, runner=flaky)
    src.prepare()
    m = Mem(1, "x", {}, [], None, None, [])
    ev = src.evidence(m, Claim("c1", "sha", "deadbee1", "deadbee1"))
    assert ev.available is False and "git failed" in ev.text and "not found" not in ev.text
    timeout = dream.GitSource(cfg, runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 1)))
    timeout.prepare()
    assert timeout.evidence(m, Claim("c1", "tag", "v0.0.1", "v0.0.1")).available is False


def test_a_negative_from_a_stale_mirror_is_not_evidence(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    m = Mem(1, "x", {}, ["proj"], None, None, [])
    stale = dream.GitSource(_cfg(tmp_path, repos, git_dir))
    stale.cfg.fetch = False
    stale.prepare()
    for c in (Claim("c1", "sha", "deadbee1", "deadbee1"), Claim("c2", "tag", "v9.9.9", "v9.9.9"),
              Claim("c3", "item", "item 99", "99"), Claim("c4", "path", "nope/missing.py", "nope/missing.py")):
        assert stale.evidence(m, c).available is False, c
    fresh = dream.GitSource(_cfg(tmp_path, repos, git_dir))
    fresh.prepare()
    assert fresh.status["proj"] == "fetched"
    for c in (Claim("c1", "sha", "deadbee1", "deadbee1"), Claim("c2", "tag", "v9.9.9", "v9.9.9"),
              Claim("c3", "item", "item 99", "99"), Claim("c4", "path", "nope/missing.py", "nope/missing.py")):
        ev = fresh.evidence(m, c)
        assert ev.available is True and ("not found" in ev.text or "no " in ev.text), c      # authoritative negative


def test_file_content_is_evidence_and_a_contradicted_claim_shows_it(tmp_path, gitrepo):
    repos, git_dir, sha, src = gitrepo
    (src / "memora" / "dream.py").write_text("def present_fn():\n    return 1\n")
    _git(src, "commit", "-qam", "add fn")
    s = dream.GitSource(_cfg(tmp_path, repos, git_dir))
    s.prepare()
    m = Mem(1, "memora/dream.py defines `present_fn` and `missing_fn`", {}, [], None, None, [])
    ev = s.evidence(m, Claim("c1", "path", "memora/dream.py", "memora/dream.py"))
    assert ev.available and "def present_fn" in ev.text
    assert "symbol `missing_fn` in the file: NOT PRESENT" in ev.text and "symbol `present_fn` in the file: def present_fn" in ev.text
    assert "at commit " in ev.text
    # a judge that follows the evidence turns the contradicted claim into `wrong`
    claims = [Claim("c1", "path", "memora/dream.py", "memora/dream.py")]
    out, *_ = dream.parse_verdicts(json.dumps({"claims": [{"id": "c1", "verdict": "wrong",
              "evidence": "symbol `missing_fn` in the file: NOT PRESENT"}], "correction": None}), claims, {"c1": ev}, m.content)
    assert out["c1"]["verdict"] == "wrong"


def test_message_claims_need_no_clone(tmp_path):
    cfg = _cfg(tmp_path)                       # no repos, no git_dir at all
    cfg.history_cmd = ["echo"]
    ev = dream.GitSource(cfg).evidence(Mem(1, "mail #16307", {}, [], None, None, []), Claim("c1", "msg", "m", "16307"))
    assert ev.available and "16307" in ev.text


# --- F5 write-time revalidation


def _plan_confirming(tmp_path, repos, git_dir, judge=None):
    return _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
                judge or _answer(lambda c, p: ("wrong", _quote_first_evidence(c, p)), correction="AUTO"))


def test_conversion_to_item_record_after_the_dry_run_blocks_the_write(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid = _add(f"The release v9.9.9 is tagged in this repo, see {sha[:12]}")
    r = _plan_confirming(tmp_path, repos, git_dir)
    assert any(p["action"] == "supersede" for p in r["findings"][0]["proposals"])
    with storage.connect() as conn:                                       # content unchanged, metadata converted
        storage.update_memory(conn, mid, metadata={"kind": "item-record"}, tags=["item-record"])
    out = dream.apply_plan(r, Limits(max_writes=10))
    assert out["writes"] == 0 and "changed" in out["skipped"][0]["why"]
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_protection_is_read_at_write_time_not_from_the_plan(tmp_path, gitrepo, monkeypatch):
    repos, git_dir, sha, _src = gitrepo
    mid = _add(f"The release v9.9.9 is tagged in this repo, see {sha[:12]}")
    r = _plan_confirming(tmp_path, repos, git_dir)
    real_add = storage.add_memory

    def add_then_convert(conn, **kw):
        res = real_add(conn, **kw)
        storage.update_memory(conn, mid, metadata={"kind": "item-record"})      # a writer races us
        return res

    monkeypatch.setattr(storage, "add_memory", add_then_convert)
    out = dream.apply_plan(r, Limits(max_writes=10))
    with storage.connect() as conn:
        refs = json.loads(conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id=?", (mid,)).fetchone()[0] or "[]")
    assert not any(x.get("edge_type") == "superseded_by" for x in refs)         # the old memory is NOT hidden
    assert any("no edge written" in s["why"] for s in out["skipped"])


def test_concurrent_edit_between_check_and_stamp_is_refused_by_the_statement(tmp_path, gitrepo, monkeypatch):
    repos, git_dir, sha, _src = gitrepo
    mid = _add(f"landed {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir), _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    real = storage.update_memory
    raced = []

    def racing(conn, memory_id, **kw):
        if kw.get("expected_row") is not None and not raced:
            raced.append(1)
            real(conn, memory_id, content="somebody else wrote this meanwhile")      # a concurrent writer wins
        return real(conn, memory_id, **kw)

    monkeypatch.setattr(storage, "update_memory", racing)
    out = dream.apply_plan(r, Limits(max_writes=10))
    with storage.connect() as conn:
        content, meta = conn.execute("SELECT content, metadata FROM memories WHERE id=?", (mid,)).fetchone()
    assert content == "somebody else wrote this meanwhile" and "verified" not in (meta or "")
    assert out["writes"] == 1 and "ConcurrentUpdateError" in out["skipped"][0]["why"] and out["applied"] == []


def test_own_stamp_does_not_invalidate_the_same_plan_and_retired_memory_is_skipped(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid = _add(f"The release v9.9.9 is tagged in this repo, see {sha[:12]}")
    r = _plan_confirming(tmp_path, repos, git_dir)
    out = dream.apply_plan(r, Limits(max_writes=10))            # annotate first, then supersede, on the same memory
    assert {a["action"] for a in out["applied"]} == {"supersede", "annotate"} and not out["skipped"]
    again = dream.apply_plan(r, Limits(max_writes=10))           # the memory is now superseded: nothing more is written
    assert again["writes"] == 0


# --- F6 duplicates are leads, never proof


def test_dup_scan_ignores_critical_token_changes_and_other_projects():
    base = "The nightly compare job listens on port {} and mails the result to the leader every morning at three"
    mk = lambda i, text, proj: Mem(i, text, {"project": proj} if proj else {}, [], None, None, [])  # noqa: E731
    same = [mk(1, base.format(8080), "a"), mk(2, base.format(8081), "a")]
    assert dream.dup_scan(same, set(), 10) == []                                # one critical number differs
    other = [mk(3, base.format(8080), "a"), mk(4, base.format(8080), "b")]
    assert dream.dup_scan(other, set(), 10) == []                               # same text, different projects
    twin = [mk(5, base.format(8080), "a"), mk(6, base.format(8080) + " ok", "a")]
    assert [(d["older"], d["newer"]) for d in dream.dup_scan(twin, set(), 10)] == [(5, 6)]


def test_dup_scan_work_is_capped():
    mems = [Mem(i, "the very same long note about the nightly job and its schedule number %d ok yes" % 7, {}, [], None, None, [])
            for i in range(1, 60)]
    assert len(dream.dup_scan(mems, set(), 5)) == 5
    assert len(dream.dup_scan(mems, set(), 5, max_compares=3)) < 5            # the work cap bites
    calls = []
    dream.dup_scan(mems, set(), 5, max_compares=10**9, expired=lambda: calls.append(1) or True)
    assert calls                                                               # the deadline is consulted inside the scan


# --- F7 mirror boundary


def _git_dir_with(tmp_path, name):
    gd = tmp_path / "gd"
    gd.mkdir(exist_ok=True)
    return gd


def test_mirror_dir_escapes_and_working_trees_are_refused(tmp_path, gitrepo):
    repos, git_dir, sha, src = gitrepo
    ran = []
    real = subprocess.run
    rn = lambda cmd, **kw: ran.append(cmd) or real(cmd, **kw)  # noqa: E731
    cases = {
        "abs": {"dir": str(src)},                                 # a working repo outside git_dir
        "dotdot": {"dir": "../src"},
        "link": {"dir": "linked.git"},
        "work": {"dir": "work"},                                  # a non-bare repo INSIDE git_dir
        "ok": {"dir": "proj.git"},
    }
    os.symlink(str(src), str(git_dir / "linked.git"))
    (git_dir / "work").mkdir()
    _git(git_dir / "work", "init", "-q")
    cfg = _cfg(tmp_path, cases, git_dir)
    s = dream.GitSource(cfg, runner=rn)
    s.prepare()
    for bad in ("abs", "dotdot", "link", "work"):
        assert s.status[bad].startswith("refused"), (bad, s.status[bad])
    assert s.status["ok"] == "fetched"
    touched = {c[c.index("-C") + 1] for c in ran if "-C" in c}
    assert touched == {str((git_dir / "proj.git").resolve())}                 # git never ran anywhere else
    assert not any("fetch" in c for c in ran if str(src) in c)


def test_fetch_disables_recursion_and_maintenance(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    ran = []
    real = subprocess.run
    s = dream.GitSource(_cfg(tmp_path, repos, git_dir), runner=lambda cmd, **kw: ran.append(cmd) or real(cmd, **kw))
    s.prepare()
    f = next(c for c in ran if "fetch" in c)
    assert "--no-recurse-submodules" in f and "gc.auto=0" in f and "maintenance.auto=false" in f


# --- P2s


def test_digest_binds_the_whole_envelope(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    _add(f"landed {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir), _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    for key, val in (("created_ts", r["created_ts"] + 10), ("run_id", "forged"), ("mode", "apply")):
        t = dict(r, **{key: val})
        p = tmp_path / "p.json"
        p.write_text(json.dumps(t, default=str))
        with pytest.raises(DreamError):
            dream.load_plan(str(p))
    t = json.loads(json.dumps(r, default=str))
    t["findings"][0]["claims"][0]["source"] = "forged evidence"
    p.write_text(json.dumps(t))
    with pytest.raises(DreamError, match="digest"):
        dream.load_plan(str(p))


def test_own_provenance_is_not_fed_back_as_claims():
    meta = {"references_checked": {"run": "abcdef123456", "at": "2026-11-07T00:00:00Z"},
            "dream": {"corrects": 5, "run": "fedcba987654"}, "sha": "1234abc"}
    got = {(c.kind, c.value) for c in dream.extract_claims("plain", meta)}
    assert got == {("sha", "1234abc")}


def test_nonfinite_and_negative_limits_are_refused(tmp_path):
    for bad in ('{"limits": {"max_seconds": NaN}}', '{"limits": {"max_seconds": Infinity}}', '{"limits": {"max_writes": -1}}'):
        p = tmp_path / "c.json"
        p.write_text(bad)
        with pytest.raises(DreamError):
            Config.load(str(p))
    p.write_text(json.dumps({"state_dir": str(tmp_path / "s")}))
    assert dream.main(["--config", str(p), "--max-memories", "-3"]) == 2


def test_report_write_is_0600_atomic_and_refuses_symlinks(tmp_path):
    out = tmp_path / "r.json"
    out.write_text("old")
    os.chmod(out, 0o644)
    dream.write_report(str(out), {"a": 1})
    assert json.loads(out.read_text()) == {"a": 1} and oct(out.stat().st_mode & 0o777) == "0o600"
    link = tmp_path / "link.json"
    os.symlink(str(out), str(link))
    with pytest.raises(DreamError, match="symlink"):
        dream.write_report(str(link), {"b": 2})
    assert json.loads(out.read_text()) == {"a": 1}


def test_unnamed_repository_negatives_are_not_evidence(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    cfg = _cfg(tmp_path, repos, git_dir)
    s = dream.GitSource(cfg)
    s.prepare()
    gone = Claim("c1", "path", "ml/train.py", "ml/train.py")
    named = Mem(1, "x", {"project": "proj"}, [], None, None, [])
    unnamed = Mem(2, "x about some other repository", {}, [], None, None, [])
    assert s.evidence(named, gone).available is True            # the note is about this repo: absence counts
    ev = s.evidence(unnamed, gone)
    assert ev.available is False and "proves nothing" in ev.text
    present = Claim("c2", "path", "memora/dream.py", "memora/dream.py")
    assert s.evidence(unnamed, present).available is True        # a positive match still counts


def test_truncated_judge_answer_is_no_answer(monkeypatch):
    import types, sys as _sys
    msg = types.SimpleNamespace(content="")
    choice = types.SimpleNamespace(finish_reason="length", message=msg)

    class FakeClient:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(
                create=lambda **k: types.SimpleNamespace(choices=[choice])))
    monkeypatch.setitem(_sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeClient))
    monkeypatch.setenv("MEMORA_DREAM_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    j = dream.OpenAIJudge()
    with pytest.raises(dream.JudgeError, match="incomplete"):
        j("p")
    choice.finish_reason, msg.content = "stop", '{"claims": []}'
    assert j("p") == '{"claims": []}'


# ----------------------------------------------------------------------------- round 3: traces for the repeated classes


def _supersede_plan(tmp_path, repos, git_dir, sha, text=None):
    mid = _add(text or f"The release v9.9.9 is tagged in this repo, see {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
             _answer(lambda c, p: ("wrong", _quote_first_evidence(c, p)), correction="AUTO"))
    assert any(p["action"] == "supersede" for p in r["findings"][0]["proposals"])
    return mid, r


def _edges(mid):
    with storage.connect() as conn:
        row = conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id=?", (mid,)).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def _count():
    with storage.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def test_deadline_crossed_during_add_memory_never_dispatches_the_edge(tmp_path, gitrepo, monkeypatch, capsys):
    import time as _t
    repos, git_dir, sha, _src = gitrepo
    mid, r = _supersede_plan(tmp_path, repos, git_dir, sha)
    real = storage.add_memory
    done = []

    def slow_add(conn, **kw):
        _t.sleep(0.8)                                   # the "backend" outlives the deadline
        res = real(conn, **kw)
        done.append(res)
        return res

    monkeypatch.setattr(storage, "add_memory", slow_add)
    out = dream.apply_plan(r, Limits(max_writes=10, max_seconds=0.4))
    print("TRACE a:", json.dumps({k: out[k] for k in ("aborted", "ambiguous", "writes_charged", "dispatch_log")}))
    assert out["aborted"] == "max_seconds" and out["ambiguous"][0]["op"] == "add_memory"
    assert out["writes_charged"] == 4                  # annotate 1 + a correction's worst case 3
    assert all(a["action"] != "supersede" for a in out["applied"])
    ops = [e["op"] for e in out["dispatch_log"] if e["dispatched"]]
    assert "guarded_edge" not in ops and "add_link" not in ops
    last = [e for e in out["dispatch_log"] if e["op"] == "add_memory"][0]
    assert last["end"] is None                           # never returned to the caller
    for _ in range(50):                                  # the abandoned call may still finish; give it time
        if done:
            break
        _t.sleep(0.1)
    assert done
    assert not any(e.get("edge_type") in ("supersedes", "superseded_by") for e in _edges(mid))
    assert not any(e.get("edge_type") == "supersedes" for e in _edges(done[0]["id"] if "id" in done[0] else done[0]["memory"]["id"]))


@pytest.mark.parametrize("race", ["content", "item-record"])
def test_race_at_the_link_boundary_hides_nothing(tmp_path, gitrepo, monkeypatch, race):
    repos, git_dir, sha, _src = gitrepo
    mid, r = _supersede_plan(tmp_path, repos, git_dir, sha)
    real = dream._guarded_edge
    fired = []

    def racing(conn, storage_mod, holder, peer, edge_type, old, raw_old, with_edge_check):
        if not fired:
            fired.append(1)                              # a writer lands AFTER our last read, BEFORE the edge
            if race == "content":
                storage.update_memory(conn, mid, content="edited by somebody else just now")
            else:
                storage.update_memory(conn, mid, metadata={"kind": "item-record"})
        return real(conn, storage_mod, holder, peer, edge_type, old, raw_old, with_edge_check)

    monkeypatch.setattr(dream, "_guarded_edge", racing)
    out = dream.apply_plan(r, Limits(max_writes=10))
    sup = [s for s in out["skipped"] if "link boundary" in s["why"]]
    print("TRACE b:", race, json.dumps({"skipped": out["skipped"], "applied": out["applied"]}),
          "edges_old", _edges(mid))
    assert len(sup) == 1 and not any(a.get("action") == "supersede" for a in out["applied"])
    assert not any(e.get("edge_type") in ("superseded_by", "supersedes") for e in _edges(mid))   # old memory still visible
    with storage.connect() as conn:
        assert not storage._is_superseded(conn, mid)
    new_id = sup[0]["new_id"]
    assert not any(e.get("edge_type") == "supersedes" for e in _edges(new_id))


def test_guarded_edge_writes_when_nothing_raced_control(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid, r = _supersede_plan(tmp_path, repos, git_dir, sha)
    out = dream.apply_plan(r, Limits(max_writes=10))
    done = [a for a in out["applied"] if a["action"] == "supersede"][0]
    assert done["edge"] == "supersedes" and "partial" not in done
    with storage.connect() as conn:
        assert storage._is_superseded(conn, mid)
    assert any(e["edge_type"] == "supersedes" and e["id"] == mid for e in _edges(done["new_id"]))


def test_forward_only_supersession_counts_as_retired(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid, r = _supersede_plan(tmp_path, repos, git_dir, sha)
    other = _add("someone else already replaced the first note")
    with storage.connect() as conn:
        storage.add_link(conn, other, mid, edge_type="supersedes", bidirectional=False)   # forward-only
    n = _count()
    out = dream.apply_plan(r, Limits(max_writes=10))
    print("TRACE retired:", json.dumps({"skipped": out["skipped"], "applied": out["applied"], "writes": out["writes"]}))
    assert out["applied"] == [] and "retired" in out["skipped"][0]["why"] and out["writes"] == 0
    assert _count() == n                                  # no correction, no second hiding edge


def test_failed_branch_and_tag_calls_after_a_resolved_sha_are_unknown_not_none(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    real = subprocess.run
    seen = []

    def runner(cmd, **kw):
        if "for-each-ref" in cmd or ("log" in cmd and "-1" in cmd):
            return subprocess.CompletedProcess(cmd, 128, "", "fatal")
        return real(cmd, **kw)

    s = dream.GitSource(_cfg(tmp_path, repos, git_dir), runner=runner)
    s.prepare()
    ev = s.evidence(Mem(1, "x", {}, ["proj"], None, None, []), Claim("c1", "sha", sha[:12], sha[:12]))
    print("TRACE c1:", ev.text)
    assert ev.available is True and sha in ev.text           # the SHA itself resolved
    assert "on branches: unknown (git failed)" in ev.text and "in tags: unknown (git failed)" in ev.text
    assert "none" not in ev.text and "commit details unavailable" in ev.text
    # the judge cannot turn the failure into a verdict by quoting an absence that is not there
    claims = [Claim("c1", "sha", sha[:12], sha[:12])]
    out, *_ = dream.parse_verdicts(json.dumps({"claims": [{"id": "c1", "verdict": "wrong",
              "evidence": "on branches: none"}], "correction": "x"}), claims, {"c1": ev}, "orig")
    assert out["c1"]["verdict"] == "unverifiable"


def test_symbol_beyond_the_read_cap_is_unknown_and_show_uses_the_resolved_commit(tmp_path, gitrepo, monkeypatch):
    repos, git_dir, sha, src = gitrepo
    (src / "memora" / "big.py").write_text("x = 1\n" * 100 + "def tail_fn():\n    pass\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "big file")
    full = _git(src, "rev-parse", "HEAD")
    real = subprocess.run
    shows = []
    s = dream.GitSource(_cfg(tmp_path, repos, git_dir), runner=lambda cmd, **kw: (shows.append(cmd) if "show" in cmd else None) or real(cmd, **kw))
    s.prepare()
    m = Mem(1, "memora/big.py has `tail_fn` and `nowhere_fn`", {}, ["proj"], None, None, [])
    claim = Claim("c1", "path", "memora/big.py", "memora/big.py")
    ev = s.evidence(m, claim)
    assert "symbol `tail_fn` in the file: def tail_fn" in ev.text and "symbol `nowhere_fn` in the file: NOT PRESENT" in ev.text
    assert any(c[-1] == f"{full}:memora/big.py" for c in shows)             # the recorded commit identifies the bytes
    assert f"at commit {full}" in ev.text
    monkeypatch.setattr(dream.GitSource, "_MAX_SHOW", 100)                   # file now over the read cap
    s2 = dream.GitSource(_cfg(tmp_path, repos, git_dir))
    s2.prepare()
    ev2 = s2.evidence(m, claim)
    print("TRACE c2:", ev2.text[-260:])
    assert "symbols unknown" in ev2.text and "NOT PRESENT" not in ev2.text


def test_truncated_file_list_makes_absence_unknown(tmp_path, gitrepo, monkeypatch):
    repos, git_dir, sha, src = gitrepo
    (src / "a.txt").write_text("a")
    (src / "b.txt").write_text("b")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "more files")
    monkeypatch.setattr(dream.GitSource, "_TREE_CAP", 1)
    s = dream.GitSource(_cfg(tmp_path, repos, git_dir))
    s.prepare()
    m = Mem(1, "x", {}, ["proj"], None, None, [])
    ev = s.evidence(m, Claim("c1", "path", "nope/missing.py", "nope/missing.py"))
    assert ev.available is False and "truncated" in ev.text


# ----------------------------------------------------------------------------- round 4: references, not truth


def _real_note_repo(src):
    (src / "src" / "clmux").mkdir(parents=True, exist_ok=True)
    (src / "src" / "cli").mkdir(parents=True, exist_ok=True)
    (src / "src" / "clmux" / "socket_server.zig").write_text("// handleWorkspaceActivityStop\n")
    (src / "src" / "cli" / "tui.zig").write_text("// isAgentActive\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "files")


NOTE_589 = ("**Findings** High - Pane-specific `activity stop` is still workspace-global. In "
            "[src/clmux/socket_server.zig](/x/src/clmux/socket_server.zig):2296, `handleWorkspaceActivityStop` always "
            "sets ws.agent_turn_open = false. The TUI dot reads src/cli/tui.zig:278.")
META_589 = {"source": "review_done", "closing_note": "Primary High finding is fixed in current code; the others may be unresolved."}
NOTE_344 = ("## Research: Most Prolific clmux Enhancements\n\n| Claim | Sources |\n|---|---|\n"
            "| Durable orchestration is top priority | review_session.zig (no serialization, critic verified) |\n"
            "Reviewer adapters are hardcoded in src/clmux/socket_server.zig and there is no decomposition.")
NOTE_30 = ("Video Support Implementation\n\n**Video Storage Module (`memora/video_storage.py`)**\n"
           "- [ ] Create `R2VideoStorage` class mirroring image_storage.py patterns\n- [ ] `upload_video()`")


def _yes_judge(prompt, timeout=None):
    """A judge as lenient as the real one was on #589/#344: confirms every reference whose file exists."""
    ids = [l.split("id=")[1].split()[0] for l in prompt.splitlines() if l.startswith("- id=")]
    rows = []
    for i in ids:
        line = _ev_line(i, prompt)
        miss = "no file matches" in line or "not found" in line
        rows.append({"id": i, "verdict": "wrong" if miss else "confirmed", "evidence": line[:40]})
    return json.dumps({"claims": rows, "correction": "rewritten note"})


def test_589_and_344_get_reference_counts_never_a_verified_stamp(tmp_path, gitrepo):
    repos, git_dir, sha, src = gitrepo
    _real_note_repo(src)
    a = _add(NOTE_589, tags=["codex"], metadata=META_589)
    b = _add(NOTE_344, tags=["clmux/research"])
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10), _yes_judge)
    by = {f["memory_id"]: f for f in r["findings"]}
    for mid in (a, b):
        meta = by[mid]["proposals"][0]["metadata"]
        assert set(meta) == {"references_checked"}                       # no `verified` key, ever
        rc = meta["references_checked"]
        assert rc["found"] >= 1 and set(rc) >= {"at", "found", "missing", "unknown", "summary"}
    assert "does NOT verify behaviour" in r["scope"]
    assert "verified" not in json.dumps([f["proposals"] for f in r["findings"]])
    # the judge was told what it is checking, and saw the whole note with its metadata
    seen = []
    _dry(_cfg(tmp_path, repos, git_dir, max_memories=10), lambda p, timeout=None: seen.append(p) or _yes_judge(p))
    p589 = next(p for p in seen if "Pane-specific" in p)
    assert "NOT checking whether anything the note says about behaviour is true" in p589
    assert "closing_note" in p589 and "tags: ['codex'" in p589 and "modality hint: note" in p589


def test_30_an_open_task_is_not_wrong_for_the_file_it_proposes(tmp_path, gitrepo):
    repos, git_dir, sha, src = gitrepo
    (src / "memora").mkdir(exist_ok=True)
    (src / "memora" / "image_storage.py").write_text("x=1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "image")
    mid = _add(NOTE_30, tags=["memora/todos"], metadata={"type": "todo", "status": "open", "category": "video-support"})
    with storage.connect() as conn:
        m = next(x for x in dream.load_memories(conn) if x.id == mid)
    assert dream.modality(m) == "open_task"
    seen = []
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10), lambda p, timeout=None: seen.append(p) or _yes_judge(p))
    f = r["findings"][0]
    assert f["modality"] == "open_task" and "modality hint: open_task" in seen[0]
    assert "wrong" not in {c["verdict"] for c in f["claims"]}            # the judge said wrong; the backstop refused it
    assert f["overall"] != "missing" and f["correction"] is None
    assert not any(p["action"] == "supersede" for p in f["proposals"])
    vid = next(c for c in f["claims"] if c["text"] == "memora/video_storage.py")
    assert vid["verdict"] == "unverifiable" and "proposes or plans" in vid["reason"]


def test_modality_hints():
    mk = lambda c, md=None, tags=None: Mem(1, c, md or {}, tags or [], None, None, [])  # noqa: E731
    assert dream.modality(mk("did x", {"type": "todo", "status": "open"})) == "open_task"
    assert dream.modality(mk("did x", {"type": "todo", "status": "done"})) == "note"
    assert dream.modality(mk("x\n- [ ] later")) == "open_task"
    assert dream.modality(mk("Proposal: add y")) == "proposal"
    assert dream.modality(mk("plain finding")) == "note"


def test_a_missing_reference_the_note_asserts_is_still_wrong(tmp_path, gitrepo):
    repos, git_dir, sha, src = gitrepo
    mid = _add("The dream pass shipped in memora/never_existed.py and is live.", tags=["memora/documents"])
    r = _dry(_cfg(tmp_path, repos, git_dir), _yes_judge)
    f = r["findings"][0]
    assert f["modality"] == "note" and f["overall"] == "missing"
    assert f["proposals"][0]["metadata"]["references_checked"]["missing"] == 1


def test_charges_follow_dispatched_writes_around_the_cap(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid, r = _supersede_plan(tmp_path, repos, git_dir, sha)
    for cap, expect_super in ((3, False), (4, True)):
        out = dream.apply_plan(json.loads(json.dumps(r, default=str)), Limits(max_writes=cap))
        ops = [e["op"] for e in out["dispatch_log"] if e["dispatched"]]
        writes = [o for o in ops if o in ("update_memory", "add_memory", "add_link", "guarded_edge")]
        print("TRACE writes cap=%d charged=%d dispatched_writes=%s deferred=%s" % (cap, out["writes_charged"], writes, out["deferred"]))
        assert out["writes_charged"] >= len(writes)                     # never more dispatched than charged
        assert out["writes_charged"] <= cap
        assert any(a["action"] == "supersede" for a in out["applied"]) is expect_super
        if not expect_super:
            assert out["deferred"][0]["why"] == "max_writes" and "add_memory" not in writes
        else:
            assert writes == ["update_memory", "add_memory", "guarded_edge", "guarded_edge"]
            assert out["writes_charged"] == 4
        if not expect_super:   # restore for the second pass: the annotate stamp is the only write so far
            pass


# ----------------------------------------------------------------------------- post-PASS mutation survivors


def test_apply_with_no_time_left_dispatches_nothing(tmp_path, gitrepo, monkeypatch):
    repos, git_dir, sha, _src = gitrepo
    _add(f"landed {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir), _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    opened = []
    real = storage.connect
    monkeypatch.setattr(storage, "connect", lambda *a, **k: opened.append(1) or real(*a, **k))
    out = dream.apply_plan(r, Limits(max_writes=10, max_seconds=0))
    assert out["aborted"] == "max_seconds" and out["writes"] == 0 and out["applied"] == []
    assert opened == [] and not any(e["dispatched"] for e in out["dispatch_log"])


def test_mirror_dir_outside_git_dir_is_refused_even_when_it_is_a_bare_repo(tmp_path, gitrepo):
    repos, git_dir, sha, src = gitrepo
    outside = tmp_path / "outside.git"
    _git(tmp_path, "clone", "-q", "--mirror", str(src), str(outside))          # a perfectly good bare repo
    cfg = _cfg(tmp_path, {"abs": {"dir": str(outside)}, "dots": {"dir": "../outside.git"}}, git_dir)
    s = dream.GitSource(cfg)
    s.prepare()
    assert s.status["abs"].startswith("refused") and s.status["dots"].startswith("refused")
    assert s.git("abs", "rev-parse", "HEAD").ok is False and s.git("dots", "rev-parse", "HEAD").ok is False


def test_llm_call_cap_holds_in_the_budget_itself():
    b = dream.Budget(Limits(max_llm_calls=2))
    assert [b.take_llm_call() for _ in range(3)] == [True, True, False] and b.stopped_by == "max_llm_calls"


# ----------------------------------------------------------------------------- store access goes through the store lock


def test_dream_has_no_raw_sqlite_connect():
    import ast, pathlib
    tree = ast.parse(pathlib.Path(dream.__file__).read_text())
    raw = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "connect" and getattr(n.func.value, "id", "") == "sqlite3"]
    assert raw == []


def test_live_store_and_db_file_are_read_on_the_shared_side_of_the_store_lock(tmp_path, monkeypatch):
    """No --db (the live store, whatever backend is configured) and --db (a file) both read through
    connect_read_only; the connection is the locked READER and cannot write."""
    from memora import backends
    _add("Item 4 note memora/a.py")
    cfgp = tmp_path / "c.json"
    cfgp.write_text(json.dumps({"state_dir": str(tmp_path / "s")}))
    seen = []
    real = dream.run_dry

    def spy(conn, *a, **k):
        seen.append(conn)
        assert isinstance(conn, backends._LockedReaderConnection)
        with pytest.raises(Exception):
            conn.execute("INSERT INTO memories (content) VALUES ('x')")
        return real(conn, *a, **k)

    monkeypatch.setattr(dream, "run_dry", spy)
    monkeypatch.delenv("MEMORA_DREAM_MODEL", raising=False)
    assert dream.main(["--config", str(cfgp)]) == 3                                  # live store path
    assert dream.main(["--config", str(cfgp), "--db", str(tmp_path / "memories.db")]) == 3   # file path
    assert len(seen) == 2


def test_readonly_file_read_holds_the_shared_lock_so_a_writer_cannot_open_meanwhile(tmp_path):
    from memora import backends
    _add("Item 4 note memora/a.py")
    db = tmp_path / "memories.db"
    conn = dream.open_readonly_file(str(db))
    try:
        with pytest.raises(RuntimeError, match="read-only connection"):    # the same thread would deadlock: refused
            storage.connect()
    finally:
        conn.close()
    storage.connect().close()                                               # released after close


def test_missing_file_is_refused_not_created(tmp_path):
    with pytest.raises(DreamError):
        dream.open_readonly_file(str(tmp_path / "nope.db"))
    assert not (tmp_path / "nope.db").exists()


# ----------------------------------------------------------------------------- Item 244: false positives, unsafe corrections

# Synthetic shapes of the first live dry run (no real identifiers).
RECORD_244 = ("Item 99 record: squash of 5c5d7ebb, reviewer PASS (8435f380). The upstream pin 74d0c72f and the "
              "upstream tag v1.4.0 belong to another project; scratch page item161-sidebar.html; "
              "old version name v0.4.16.1 for the first build.")


def _by_text(claims):
    return {c.text: c for c in claims}


def test_244_claims_know_where_absence_proves_nothing():
    got = _by_text(dream.extract_claims(RECORD_244))
    assert got["5c5d7ebb"].ctx == "history" and got["8435f380"].ctx == "history"      # squash / review PASS
    assert got["74d0c72f"].ctx == "foreign" and got["v1.4.0"].ctx == "foreign"        # another project
    assert got["item161-sidebar.html"].ctx == "bare"                                  # a file name, not a path
    assert got["v0.4.16.1"].ctx == "" and got["v0.4.16.1"].asserts is False           # absence is only "missing"
    more = _by_text(dream.extract_claims("Round 2 fixed abcdef12. The file memora/dream.py is tagged v0.5.7.7; "
                                         "landed on dev as 1234abcd5678."))
    assert more["abcdef12"].ctx == "history"
    assert more["v0.5.7.7"].asserts and more["1234abcd5678"].asserts and more["memora/dream.py"].ctx == ""


def test_244_any_restricted_mention_restricts_the_claim():
    got = _by_text(dream.extract_claims("squash of 5c5d7ebb. Later, 5c5d7ebb landed on dev."))
    assert got["5c5d7ebb"].ctx == "history" and not got["5c5d7ebb"].asserts
    got = _by_text(dream.extract_claims("5c5d7ebb landed on dev. Also 5c5d7ebb is mentioned here."))
    assert got["5c5d7ebb"].ctx == "" and not got["5c5d7ebb"].asserts      # every unrestricted mention must assert


@pytest.mark.parametrize("ctx", ["history", "foreign", "bare"])
@pytest.mark.parametrize("verdict", ["wrong", "stale"])
def test_244_guard_makes_restricted_references_unverifiable(ctx, verdict):
    c = Claim("c1", "sha", "abc1234", "abc1234", ctx=ctx, asserts=True)
    parsed = {"c1": {"verdict": verdict, "evidence": "q", "reason": ""}}
    dream._guard_verdicts([c], parsed, lambda c: c.asserts)
    assert parsed["c1"]["verdict"] == "unverifiable"


def test_244_guard_wrong_needs_an_assertion_stale_is_never_absence():
    plain = Claim("c1", "tag", "v1.0.0", "v1.0.0")
    asserted = Claim("c2", "tag", "v2.0.0", "v2.0.0", asserts=True)
    parsed = {"c1": {"verdict": "wrong", "evidence": "q", "reason": ""},
              "c2": {"verdict": "wrong", "evidence": "q", "reason": ""}}
    dream._guard_verdicts([plain, asserted], parsed, lambda c: c.asserts)
    assert parsed["c1"]["verdict"] == "missing" and parsed["c2"]["verdict"] == "wrong"
    parsed = {"c1": {"verdict": "stale", "evidence": "q", "reason": ""}}
    dream._guard_verdicts([plain], parsed, lambda c: c.asserts)
    assert parsed["c1"]["verdict"] == "unverifiable"


def test_244_the_judge_cannot_answer_missing():
    claims, ev = _claim_and_ev()
    out, _c, prob = dream.parse_verdicts(
        json.dumps({"claims": [{"id": "c1", "verdict": "missing", "evidence": "exists on dev"}], "correction": None}),
        claims, ev, "n")
    assert out["c1"]["verdict"] == "unverifiable" and prob


def test_244_foreign_and_history_references_are_never_wrong_or_corrected(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid = _add(RECORD_244)
    judge = _answer(lambda c, p: ("wrong", _quote_first_evidence(c, p)), correction="AUTO")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10), judge)
    f = next(x for x in r["findings"] if x["memory_id"] == mid)
    verdicts = {c["text"]: c["verdict"] for c in f["claims"]}
    assert "wrong" not in verdicts.values() and "stale" not in verdicts.values(), verdicts
    for t in ("5c5d7ebb", "8435f380", "74d0c72f", "v1.4.0", "item161-sidebar.html"):
        assert verdicts[t] == "unverifiable", (t, verdicts)
    assert verdicts["v0.4.16.1"] == "missing"                       # reported, nothing more
    assert f["overall"] == "missing"
    assert f["correction"] is None and f["correction_rejected"]
    assert all(p["action"] == "annotate" for p in f["proposals"])
    ann = f["proposals"][0]["metadata"]["references_checked"]
    assert ann["missing"] >= 1 and ann["summary"] == "missing"


def test_244_a_bare_file_name_found_in_the_mirror_is_still_confirmed(tmp_path, gitrepo):
    repos, git_dir, _sha, _src = gitrepo
    mid = _add("See dream.py for details of the pass, and nothing_here.py too.")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
             _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    f = next(x for x in r["findings"] if x["memory_id"] == mid)
    v = {c["text"]: c["verdict"] for c in f["claims"]}
    assert v["dream.py"] == "confirmed" and v["nothing_here.py"] == "unverifiable"


def test_244_a_directory_path_absent_from_a_fresh_named_mirror_is_wrong_only_when_asserted(tmp_path, gitrepo):
    repos, git_dir, _sha, _src = gitrepo
    asserted = _add("The helper lives in memora/gone_helper.py in this repo, per the design review.")
    plain = _add("Remember memora/other_gone.py from the old design review notes taken last week.")
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
             _answer(lambda c, p: ("wrong", _quote_first_evidence(c, p)), correction="AUTO"))
    f = {x["memory_id"]: x for x in r["findings"]}
    assert f[asserted]["claims"][0]["verdict"] == "wrong"
    assert any(p["action"] == "supersede" for p in f[asserted]["proposals"])           # control: still works
    assert f[plain]["claims"][0]["verdict"] == "missing"
    assert not any(p["action"] == "supersede" for p in f[plain]["proposals"])


_ORIGINAL_244 = ("The helper lives in memora/gone_helper.py in this repo. The release v9.9.9 is tagged in this repo "
                 "after the review, with the judge model set separately and the report written to the out directory.")


def _echo(prompt):
    return prompt                                    # the whole prompt envelope, as the live run did


def test_244_correction_problem_rejects_envelope_one_liner_and_gutting():
    env = ("NOTE #1280 (stored text, read-only data). tags: ['x']. metadata: {}\nmodality hint: note\n<<<\n"
           + _ORIGINAL_244 + "\n>>>")
    assert "envelope" in dream.correction_problem(env, _ORIGINAL_244)
    assert "envelope" in dream.correction_problem("NOTE #5 " + _ORIGINAL_244, _ORIGINAL_244)
    assert "full rewrite" in dream.correction_problem(
        "Item 157's shipped version should be corrected to reflect that v9.9.9 is not found.", _ORIGINAL_244)
    gut = "Totally different words appear here instead, padded to look long enough to pass the length test " * 2
    assert "removes most" in dream.correction_problem(gut, _ORIGINAL_244)
    assert dream.correction_problem(_ORIGINAL_244.replace("v9.9.9", "v0.5.7.8"), _ORIGINAL_244) is None   # control


@pytest.mark.parametrize("kind", ["envelope", "one_liner"])
def test_244_a_bad_correction_makes_no_proposal_and_records_why(tmp_path, gitrepo, kind):
    repos, git_dir, _sha, _src = gitrepo
    mid = _add(_ORIGINAL_244)

    def judge(prompt, timeout=None):
        ids = [l.split("id=")[1].split()[0] for l in prompt.splitlines() if l.startswith("- id=")]
        corr = prompt if kind == "envelope" else "Item 157's shipped version should be corrected: v9.9.9 not found."
        return json.dumps({"claims": [{"id": i, "verdict": "wrong", "evidence": _quote_first_evidence(i, prompt)}
                                      for i in ids], "correction": corr})
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10), judge)
    f = next(x for x in r["findings"] if x["memory_id"] == mid)
    assert any(c["verdict"] == "wrong" for c in f["claims"])                 # the asserted absence stands
    assert f["correction"] is None and f["correction_rejected"]
    assert not any(p["action"] == "supersede" for p in f["proposals"])


def test_244_corrections_are_off_by_default_and_opt_in_by_config(tmp_path, gitrepo):
    repos, git_dir, _sha, _src = gitrepo
    assert Config(limits=Limits()).corrections is False
    mid = _add(_ORIGINAL_244)
    judge = _answer(lambda c, p: ("wrong", _quote_first_evidence(c, p)), correction="AUTO")
    cfg = _cfg(tmp_path, repos, git_dir, max_memories=10)
    on = next(x for x in _dry(cfg, judge)["findings"] if x["memory_id"] == mid)
    assert any(p["action"] == "supersede" for p in on["proposals"])
    cfg.corrections = False
    off = next(x for x in _dry(cfg, judge)["findings"] if x["memory_id"] == mid)
    assert not any(p["action"] == "supersede" for p in off["proposals"])
    assert "corrections are off" in off["correction_rejected"]
    assert any(p["action"] == "annotate" for p in off["proposals"])          # the report still annotates
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"corrections": True}))
    assert Config.load(str(f)).corrections is True
    f.write_text(json.dumps({"corrections": "yes"}))
    assert Config.load(str(f)).corrections is False                            # only a real true opts in


def test_244_apply_refuses_a_bad_supersede_proposal_from_an_older_plan(tmp_path, gitrepo):
    repos, git_dir, _sha, _src = gitrepo
    mid = _add(_ORIGINAL_244)
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10),
             _answer(lambda c, p: ("wrong", _quote_first_evidence(c, p)), correction="AUTO"))
    sup = next(p for p in r["findings"][0]["proposals"] if p["action"] == "supersede")
    sup["content"] = "Item 157's shipped version should be corrected."          # e.g. a plan from the old version
    before = _count()
    out = dream.apply_plan(r, Limits(max_writes=20))
    assert _count() == before
    assert any("correction refused" in s.get("why", "") for s in out["skipped"])
    assert not any(a["action"] == "supersede" for a in out["applied"])
    assert out["writes_charged"] == 1                                            # only the annotate was charged


def _show(text, f):
    """Program output for the reviewer: `DREAM_SHOW=1 pytest -s -k 244`."""
    if os.environ.get("DREAM_SHOW"):
        print("\nNOTE:", text)
        for c in dream.extract_claims(text, {"project": "proj"}):
            print("  extracted:", {k: getattr(c, k) for k in ("kind", "text", "ctx", "asserts", "pre", "post")})
        for c in f["claims"]:
            print("  claim:", c["text"], "ctx=%r" % c["ctx"], "verdict=%s" % c["verdict"], "| evidence:", c["source"][:70].replace("\n", " "))
        print("  correction_rejected:", f["correction_rejected"], "| proposals:", [p["action"] for p in f["proposals"]])


def _wrong_judge(correction="AUTO"):
    return _answer(lambda c, p: ("wrong", _quote_first_evidence(c, p)), correction=correction)


def _find(r, mid):
    return next(x for x in r["findings"] if x["memory_id"] == mid)


@pytest.mark.parametrize("text", [
    "The old build name v0.4.16.1 is not a tag in any repository we ever used, per the release review notes.",
    "Notes from the vendor call: WidgetSDK released v1.4.0 last month, according to their changelog posted online.",
    "In the past v9.9.9 was tagged on the first try, but later that tag was removed during the cleanup of tags.",
    "proj uses WidgetSDK tag v1.4.0 for builds, as noted in the planning review notes of last week, fine.",
    "this repo uses WidgetSDK tag v1.4.0 for builds, as noted in the planning review notes of last week.",
    "WidgetSDK tagged v1.4.0 in proj builds only, as noted in the planning review notes of last week, ok.",
    "The proj team says WidgetSDK is tagged v1.4.0 upstream, noted in the planning review of last week.",
    "The release v1.4.0 is tagged in its own repository while proj merely builds with it, per last week review.",
    "The release v1.4.0 is tagged by WidgetSDK in the vendor repository; proj is only a consumer of the tag.",
])
def test_244_negated_historical_or_unknown_subject_membership_is_never_wrong(tmp_path, gitrepo, text):
    repos, git_dir, _sha, _src = gitrepo
    mid = _add(text, metadata={"project": "proj"})
    f = _find(_dry(_cfg(tmp_path, repos, git_dir, max_memories=10), _wrong_judge()), mid)
    _show(text, f)
    vs = {c["verdict"] for c in f["claims"] if c["kind"] == "tag"}
    assert vs == {"missing"} or vs == {"unverifiable"}, f["claims"]
    assert not any(p["action"] == "supersede" for p in f["proposals"])


@pytest.mark.parametrize("text", [
    "The release v9.9.9 is tagged in the proj repo, as agreed in the planning review notes of last week.",
    "The release v9.9.9 is tagged in this repository, as agreed in the planning review notes of last week.",
    "Tagged v9.9.9 in proj, as agreed in the planning review notes of last week, a clear and affirmative fact.",
    "The change landed on dev as deadbeef12 according to the planning review notes of last week, a clear fact.",
])
def test_244_affirmative_membership_in_the_named_repo_is_still_wrong_control(tmp_path, gitrepo, text):
    repos, git_dir, _sha, _src = gitrepo
    mid = _add(text, metadata={"project": "proj"})
    f = _find(_dry(_cfg(tmp_path, repos, git_dir, max_memories=10), _wrong_judge()), mid)
    _show(text, f)
    assert any(c["verdict"] == "wrong" for c in f["claims"]), f["claims"]
    assert any(p["action"] == "supersede" for p in f["proposals"])


def _write_plan(tmp_path, report, drop_rules=False):
    rep = dict(report)
    if drop_rules:
        rep.pop("rules")
    rep["digest"] = dream.plan_digest(rep)
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(rep, default=str))
    return str(p)


def _cfg_file(tmp_path, **extra):
    f = tmp_path / "dream.json"
    f.write_text(json.dumps({"state_dir": str(tmp_path / "state"), **extra}))
    return str(f)


def _unsafe_plan(tmp_path, gitrepo_):
    repos, git_dir, _sha, _src = gitrepo_
    mid = _add(_ORIGINAL_244)
    r = _dry(_cfg(tmp_path, repos, git_dir, max_memories=10), _wrong_judge())
    return mid, r


def test_244_main_apply_with_default_config_writes_no_corrected_copy(tmp_path, gitrepo, capsys):
    mid, r = _unsafe_plan(tmp_path, gitrepo)
    plan = _write_plan(tmp_path, r)
    before = _count()
    assert dream.main(["--config", _cfg_file(tmp_path), "--apply", "--plan", plan]) == 0
    out = json.loads(capsys.readouterr().out)
    assert _count() == before and not any(a["action"] == "supersede" for a in out["applied"])
    assert any("corrections are off" in x.get("why", "") for x in out["skipped"])
    assert out["writes_charged"] == 1                                       # only the annotation
    assert not any(e.get("edge_type") in ("supersedes", "superseded_by") for e in _edges(mid))


def test_244_main_apply_with_corrections_on_still_supersedes_control(tmp_path, gitrepo, capsys):
    mid, r = _unsafe_plan(tmp_path, gitrepo)
    plan = _write_plan(tmp_path, r)
    assert dream.main(["--config", _cfg_file(tmp_path, corrections=True), "--apply", "--plan", plan]) == 0
    out = json.loads(capsys.readouterr().out)
    assert any(a["action"] == "supersede" for a in out["applied"])


def test_244_a_legacy_plan_without_the_new_rules_is_refused(tmp_path, gitrepo, capsys):
    _mid, r = _unsafe_plan(tmp_path, gitrepo)
    plan = _write_plan(tmp_path, r, drop_rules=True)
    before = _count()
    assert dream.main(["--config", _cfg_file(tmp_path, corrections=True), "--apply", "--plan", plan]) == 2
    assert "older dream rules" in capsys.readouterr().err and _count() == before


def test_244_apply_refuses_a_supersede_backed_only_by_restricted_claims(tmp_path, gitrepo, capsys):
    _mid, r = _unsafe_plan(tmp_path, gitrepo)
    for c in r["findings"][0]["claims"]:
        c["ctx"] = "history"                                   # a legacy-style verdict on a pre-squash reference
    plan = _write_plan(tmp_path, r)
    before = _count()
    assert dream.main(["--config", _cfg_file(tmp_path, corrections=True), "--apply", "--plan", plan]) == 0
    out = json.loads(capsys.readouterr().out)
    assert _count() == before and any("no unrestricted" in x.get("why", "") for x in out["skipped"])


# ----------------------------------------------------------------------------- Item 250: the Claude Code judge


def _fake_claude(tmp_path, body):
    """A fake `claude` script (never the real one). `body` is Python run with sys/os/json imported."""
    p = tmp_path / "fakeclaude"
    p.write_text("#!" + __import__("sys").executable + "\nimport sys, os, json\n" + body)
    p.chmod(0o755)
    return str(p)


def _cc(monkeypatch, tmp_path, body, model="haiku"):
    monkeypatch.setenv("MEMORA_DREAM_MODEL", model)
    monkeypatch.setenv("MEMORA_DREAM_CLAUDE_BIN", _fake_claude(tmp_path, body))
    return dream.ClaudeCodeJudge()


_OK = 'sys.stdin.read(); print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ANSWER"}))\n'


def test_claude_judge_success(monkeypatch, tmp_path):
    assert _cc(monkeypatch, tmp_path, _OK)("p") == "ANSWER"


@pytest.mark.parametrize("body,match", [
    ('sys.stdin.read(); print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "x"}))', "is_error"),
    ('sys.stdin.read(); print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False, "result": "x"}))', "subtype"),
    ('sys.stdin.read(); print(json.dumps({"type": "result", "subtype": "success", "is_error": False}))', "no text"),
    ('sys.stdin.read(); print("{\\"type\\": \\"result\\"")', "not JSON"),
    ('sys.stdin.read(); print("hello")', "not JSON"),
    ('sys.stdin.read(); print("[1]")', "no result object"),
    ('sys.stdin.read(); print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "  "}))', "no text"),
    ('sys.stdin.read(); sys.stdout.buffer.write(b"\\xff\\xfe")', "not JSON"),
    ('sys.stdin.read(); sys.stderr.write("ANSWER"); sys.exit(3)', "status 3"),
    ('sys.stdin.read(); sys.stderr.write("ANSWER")', "not JSON"),
])
def test_claude_judge_failures(monkeypatch, tmp_path, body, match):
    j = _cc(monkeypatch, tmp_path, body + "\n")
    with pytest.raises(dream.JudgeError, match=match) as ei:
        j("p")
    assert "ANSWER" not in str(ei.value)   # stderr is never the answer nor echoed


def test_claude_judge_nonzero_exit_ignores_good_stdout(monkeypatch, tmp_path):
    j = _cc(monkeypatch, tmp_path, _OK + "sys.exit(1)\n")
    with pytest.raises(dream.JudgeError, match="status 1"):
        j("p")


def test_claude_judge_requires_model_and_binary(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORA_DREAM_MODEL", raising=False)
    with pytest.raises(dream.JudgeError, match="MEMORA_DREAM_MODEL"):
        dream.ClaudeCodeJudge()
    monkeypatch.setenv("MEMORA_DREAM_MODEL", "haiku")
    monkeypatch.setenv("MEMORA_DREAM_CLAUDE_BIN", str(tmp_path / "nope"))
    with pytest.raises(dream.JudgeError, match="not found"):
        dream.ClaudeCodeJudge()


def test_claude_judge_prompt_on_stdin_not_argv_and_flags(monkeypatch, tmp_path):
    out = tmp_path / "seen.json"
    j = _cc(monkeypatch, tmp_path,
            f'd = sys.stdin.read()\nopen({str(out)!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "stdin": d, "cwd": os.getcwd(), '
            f'"empty": os.listdir("."), "pgid_own": os.getpgrp() == os.getpid()}}))\n' + _OK.split("; ", 1)[1])
    secret = "SECRET-PROMPT-" + "x" * 300000    # far over any argv limit
    assert j(secret) == "ANSWER"
    seen = json.loads(out.read_text())
    assert seen["stdin"] == secret and not any("SECRET-PROMPT" in a for a in seen["argv"])
    a = seen["argv"]
    assert "-p" in a and a[a.index("--model") + 1] == "haiku" and a[a.index("--output-format") + 1] == "json"
    assert a[a.index("--system-prompt") + 1] == dream._SYSTEM
    assert a[a.index("--tools") + 1] == "" and "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert "--safe-mode" in a and "disableAllHooks" in a[a.index("--settings") + 1]
    assert "--bare" not in a
    assert seen["empty"] == [] and seen["pgid_own"] is True
    assert not os.path.exists(seen["cwd"])      # private cwd removed afterwards
    assert os.path.realpath(seen["cwd"]) != os.path.realpath(os.getcwd())


def test_claude_judge_env_scrubbed(monkeypatch, tmp_path):
    out = tmp_path / "env.json"
    leaks = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL",
             "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
             "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK")
    for k in leaks:
        monkeypatch.setenv(k, "leak")
    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    monkeypatch.setenv("KEEPME", "1")
    j = _cc(monkeypatch, tmp_path, f'open({str(out)!r}, "w").write(json.dumps(dict(os.environ)))\n' + _OK)
    j("p")
    env = json.loads(out.read_text())
    assert not set(leaks) & set(env)
    assert env["DISABLE_AUTOUPDATER"] == "1" and env["KEEPME"] == "1"
    assert all(os.environ[k] == "leak" for k in leaks)   # the parent is untouched


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_claude_judge_timeout_kills_group(monkeypatch, tmp_path):
    import time
    pids = tmp_path / "pids"
    body = (f'import subprocess\nc = subprocess.Popen(["sleep", "60"])\n'
            f'open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{c.pid}}")\ntime.sleep(60)\n').replace("import subprocess", "import subprocess, time")
    j = _cc(monkeypatch, tmp_path, body)
    t0 = time.time()
    with pytest.raises(dream.JudgeError, match="timed out"):
        j("p", timeout=1.0)
    assert time.time() - t0 < 15
    parent, child = map(int, pids.read_text().split())
    for _ in range(50):
        if not _alive(parent) and not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(parent) and not _alive(child)    # no orphan, grandchild included


def test_claude_judge_orphan_after_normal_exit_is_killed(monkeypatch, tmp_path):
    import time
    pidf = tmp_path / "bg"
    body = (f'import subprocess\nc = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\nopen({str(pidf)!r}, "w").write(str(c.pid))\n' + _OK)
    assert _cc(monkeypatch, tmp_path, body)("p") == "ANSWER"
    pid = int(pidf.read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid)


def test_judge_selector(monkeypatch, tmp_path):
    import sys as _sys
    import types
    monkeypatch.setenv("MEMORA_DREAM_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setitem(_sys.modules, "openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
    monkeypatch.setenv("MEMORA_DREAM_CLAUDE_BIN", _fake_claude(tmp_path, _OK))
    monkeypatch.delenv("MEMORA_DREAM_JUDGE", raising=False)
    assert type(dream.make_judge()) is dream.OpenAIJudge
    monkeypatch.setenv("MEMORA_DREAM_JUDGE", "openai")
    assert type(dream.make_judge()) is dream.OpenAIJudge
    monkeypatch.setenv("MEMORA_DREAM_JUDGE", "claude-code")
    assert type(dream.make_judge()) is dream.ClaudeCodeJudge
    for bad in ("Claude-Code", "anthropic", "claude"):
        monkeypatch.setenv("MEMORA_DREAM_JUDGE", bad)
        with pytest.raises(dream.JudgeError, match="unknown MEMORA_DREAM_JUDGE"):
            dream.make_judge()


def test_judge_selector_no_fallback(monkeypatch, tmp_path):
    import sys as _sys
    import types
    monkeypatch.setenv("MEMORA_DREAM_MODEL", "m")
    # claude-code selected, binary missing, OpenAI fully configured: still an error, never OpenAI
    monkeypatch.setitem(_sys.modules, "openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("MEMORA_DREAM_JUDGE", "claude-code")
    monkeypatch.setenv("MEMORA_DREAM_CLAUDE_BIN", str(tmp_path / "nope"))
    with pytest.raises(dream.JudgeError, match="not found"):
        dream.make_judge()
    # openai selected, key missing, a working claude present: still an error, never claude
    monkeypatch.setenv("MEMORA_DREAM_JUDGE", "openai")
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("MEMORA_DREAM_CLAUDE_BIN", _fake_claude(tmp_path, _OK))
    with pytest.raises(dream.JudgeError, match="OPENAI_API_KEY"):
        dream.make_judge()


def test_judge_unknown_selector_fails_closed_in_main(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORA_DREAM_JUDGE", "bogus")
    monkeypatch.setenv("MEMORA_DREAM_MODEL", "m")
    with pytest.raises(dream.JudgeError, match="bogus"):
        dream.make_judge()


def test_claude_judge_event_array_and_relative_binary(monkeypatch, tmp_path):
    ev = ('sys.stdin.read(); print(json.dumps([{"type": "system"}, {"type": "result", "subtype": "success", '
          '"is_error": False, "result": "ARR"}]))\n')
    assert _cc(monkeypatch, tmp_path, ev)("p") == "ARR"
    script = _fake_claude(tmp_path, _OK)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMORA_DREAM_CLAUDE_BIN", "./" + os.path.basename(script))
    j = dream.ClaudeCodeJudge()
    assert os.path.isabs(j.binary) and j("p") == "ANSWER"   # still found after the child's cwd changes


def test_claude_judge_subtype_not_echoed(monkeypatch, tmp_path):
    j = _cc(monkeypatch, tmp_path, 'sys.stdin.read(); print(json.dumps({"type": "result", "subtype": "SECRETWORDS", "is_error": False, "result": "x"}))\n')
    with pytest.raises(dream.JudgeError) as ei:
        j("p")
    assert "SECRETWORDS" not in str(ei.value)
