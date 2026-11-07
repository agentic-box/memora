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
    return Config(limits=Limits(**limits), git_dir=git_dir, repos=repos or {}, state_dir=tmp_path / "state")


def _add(content, tags=None, metadata=None):
    with storage.connect() as conn:
        r = storage.add_memory(conn, content=content, tags=tags or [], metadata=metadata or {})
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
    def judge(prompt):
        ids = [l.split("id=")[1].split()[0] for l in prompt.splitlines() if l.startswith("- id=")]
        out = []
        for cid in ids:
            v, quote = claims_verdicts(cid, prompt)
            out.append({"id": cid, "verdict": v, "evidence": quote})
        return json.dumps({"claims": out, "correction": correction})
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
        json.dumps({"claims": [{"id": "c1", "verdict": "confirmed", "evidence": "exists on dev"}]}), claims, ev, "n")
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

    def boom(_p):
        raise RuntimeError("provider down")

    for judge in (boom, None, lambda _p: "garbage"):
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
    bad = _add("The dream pass lives in memora/missing_file.py and shipped as v9.9.9", metadata={"project": None})
    cfg = _cfg(tmp_path, repos, git_dir)
    before = _state()

    def verdicts(cid, prompt):
        line = _ev_line(cid, prompt)
        missing = ("none" in line) or ("not found" in line) or ("does not exist" in line)
        return ("wrong" if missing else "confirmed", line[:40])

    judge = _answer(verdicts, correction="The dream pass lives in memora/dream.py")
    # claims for `bad` carry 'none'/'does not exist' evidence; the judge quotes them
    r = _dry(cfg, judge)
    assert _state() == before                                    # dry run: no write at all
    by_id = {f["memory_id"]: f for f in r["findings"]}
    assert by_id[good]["overall"] == "confirmed"
    assert by_id[good]["proposals"][0]["metadata"]["verified"]["how"] == "git+llm"
    assert by_id[bad]["overall"] == "wrong"
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
    assert out["writes"] >= 3 and not out["skipped"]
    with storage.connect() as conn:
        n_after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        content_after = {i: c for i, c in conn.execute("SELECT id, content FROM memories")}
        refs = {i: json.loads(r_ or "[]") for i, r_ in conn.execute("SELECT memory_id, related FROM memories_crossrefs")}
        meta_good = json.loads(conn.execute("SELECT metadata FROM memories WHERE id=?", (good,)).fetchone()[0])
    assert n_after == n_before + 1                                # one corrected memory added, none deleted
    assert all(content_after[i] == content_before[i] for i in content_before)   # no in-place content edit
    assert "verified" in meta_good
    new_id = [a["new_id"] for a in out["applied"] if a.get("new_id")][0]
    assert any(x.get("edge_type") == "supersedes" and x["id"] == bad for x in refs[new_id])
    assert any(x.get("edge_type") == "superseded_by" and x["id"] == new_id for x in refs[bad])


def test_item_record_is_never_superseded_only_linked_related(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    rec = _add(f"Item 7 landed as {sha[:12]} and shipped v9.9.9", tags=["item-record"], metadata={"kind": "item-record"})
    cfg = _cfg(tmp_path, repos, git_dir)
    judge = _answer(lambda cid, p: ("wrong", _quote_first_evidence(cid, p)), correction="Item 7 landed, not v9.9.9")
    r = _dry(cfg, judge)
    acts = [p["action"] for p in r["findings"][0]["proposals"]]
    assert "supersede" not in acts and "correct_related" in acts
    out = dream.apply_plan(r, Limits(max_writes=10))
    assert [a["edge"] for a in out["applied"] if "edge" in a] == ["related_to"]


def test_apply_refuses_edited_plan_and_changed_memory(tmp_path, gitrepo):
    repos, git_dir, sha, _src = gitrepo
    mid = _add(f"landed {sha[:12]}")
    r = _dry(_cfg(tmp_path, repos, git_dir), _answer(lambda c, p: ("confirmed", _quote_first_evidence(c, p))))
    path = tmp_path / "p.json"
    r["findings"][0]["proposals"][0]["metadata"]["verified"]["how"] = "tampered"
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

    def judge(p):
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
             lambda p: calls.append(p) or "{}", clock=lambda: float(next(t)))
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
        Mem(4, "Item 7 checked memora/z.py", {"verified": {"at": dream._iso(now - 86400)}}, [], None, None, []),
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
    assert {"duplicate": [a, b], "edge": "supersedes"} in out["applied"]


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
    r = _dry(_cfg(tmp_path, repos, git_dir), lambda p: "{}")
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
