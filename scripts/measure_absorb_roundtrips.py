#!/usr/bin/env python3
"""Measure absorb's D1 request count and per-phase wall time, offline.

Never touches a live store. The database is a scratch SQLite file driven
through the FakeD1Connection test double (tests/conftest.py), which keeps the
production D1 semantics that matter here: one statement == one HTTPS POST,
statement autocommit, no transactions, no FTS5. Each statement sleeps
--d1-latency seconds to stand in for the round trip.

Scenario (the shape of an update-heavy absorb call):
  - a store of --rows background memories (default 964, the live size) plus
    supersession chains of length 3 and 5 and a few related-target memories;
  - one absorb of 9 facts: 3 new (two of them similar enough to consolidate),
    3 RELATED to existing memories, 3 UPDATE (supersede). The fake classifier
    always picks the OLDEST candidate, i.e. a stale chain version, so every
    UPDATE must be re-resolved to its chain's current leaf.

LLM and embedding calls are faked with fixed sleeps (--llm-latency,
--embed-latency) so their share is visible but deterministic. The classifier
answers from a marker in each fact. The supersede verifier (when the code
has one) judges the text it is shown, the LEAF's text:
  --scenario confirm       every leaf is the same entity: all 3 supersede
                           (the pre-gate code takes the same decisions, so
                           --compare against a pre-gate run must match);
  --scenario leaf-differs  chainC's leaf is marked a different entity: the
                           verifier rejects it and chainC's fact is linked
                           RELATED instead (pre-gate code would supersede it).
The run ASSERTS the expected actions for the scenario, and with
--compare BASELINE.json that the decisions equal a previous --json run.

Default latencies: D1 0.2 s/request (memora #973 measured ~20 s for ~100
requests on the live store), embeddings 0.1 s/request (bge-m3 on the M1 from
deploy-host, measured 2026-09-23), LLM 2.0 s/call (assumed for gpt-4o-mini via
OpenRouter; pass the real figure when known).

Usage:
  ./.venv/bin/python scripts/measure_absorb_roundtrips.py [--rows 964]
      [--d1-latency 0.2] [--embed-latency 0.1] [--llm-latency 2.0] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("MEMORA_VECTOR_SCAN_PAGE_SIZE", "100")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memora  # noqa: E402
import memora.storage as storage  # noqa: E402
from tests.conftest import FakeD1Backend, FakeD1Connection  # noqa: E402


class LatencyFakeD1Connection(FakeD1Connection):
    latency = 0.0

    def execute(self, sql, params=None):
        cur = super().execute(sql, params)
        if self.latency and not self._is_savepoint(sql):
            time.sleep(self.latency)
        return cur


class LatencyFakeD1Backend(FakeD1Backend):
    def connect(self, *, check_same_thread: bool = True):
        conn = LatencyFakeD1Connection(self.db_path, transactional=self.transactional)
        self.connections.append(conn)
        return conn


def _topic_of(text: str) -> str:
    start = text.find("[topic:")
    if start == -1:
        return "none"
    return text[start + 7 : text.index("]", start)]


def fake_vector(content: str) -> dict:
    # Same topic => cosine 1/(1+0.6^2) ~= 0.735: above the 0.35 classify
    # threshold, below the 0.85 auto-duplicate threshold, above the 0.55
    # consolidation threshold. Different topic => 0.
    uniq = hashlib.sha1(content.encode()).hexdigest()[:12]
    return {f"topic:{_topic_of(content)}": 1.0, f"uniq:{uniq}": 0.6}


def install_fakes(embed_latency: float, llm_latency: float) -> None:
    def embed(content, metadata, tags):
        time.sleep(embed_latency)
        return fake_vector(content)

    def embed_batch(entries, model):
        time.sleep(embed_latency)
        return [fake_vector(e["content"]) for e in entries]

    def classify(fact, match_data):
        time.sleep(llm_latency)
        rel = fact[fact.index("[expect:") + 8 : fact.index("]", fact.index("[expect:"))]
        stale = min(m["id"] for m in match_data)  # the oldest version: stale
        return [{"memory_id": stale, "relationship": rel, "reason": "bench"}], []

    def consolidate(group, context=None):
        time.sleep(llm_latency)
        return " / ".join(group)

    def verify(new_fact, old_content, **kwargs):
        time.sleep(llm_latency)
        same = "[entity:other]" not in old_content
        return {"verdict": "supersede" if same else "related", "same_project": True,
                "same_entity": same, "fully_replaces": same, "related": True,
                "reason": "bench: same entity" if same else "bench: leaf is a different entity"}

    storage._compute_embedding = embed
    storage._compute_embeddings_batch = embed_batch
    storage._classify_fact_against_matches = classify
    storage._consolidate_facts_llm = consolidate
    if hasattr(storage, "_verify_absorb_supersede_llm"):
        storage._verify_absorb_supersede_llm = verify


def _insert(conn, content: str) -> int:
    # Direct seed: no crossref scan per row (that would be O(n^2) and is not
    # what is being measured). Crossref rows are written empty.
    cur = conn.execute(
        "INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, ?, ?, ?)",
        (content, None, "[]", "2026-09-01 00:00:00"),
    )
    mid = int(cur.lastrowid)
    storage._upsert_embedding(conn, mid, fake_vector(content))
    return mid


def seed(conn, rows: int, scenario: str) -> dict:
    for i in range(rows):
        _insert(conn, f"[topic:bg{i}] background memory {i} about subsystem {i % 37}")
    chains = {}
    for name, length in (("chainA", 3), ("chainB", 5), ("chainC", 3)):
        ids = []
        for v in range(length):
            other = scenario == "leaf-differs" and name == "chainC" and v == length - 1
            marker = " [entity:other] a different component's setting" if other else ""
            ids.append(_insert(conn, f"[topic:{name}] {name} setting version {v}{marker}"))
            if v:
                storage.add_link(conn, ids[-1], ids[-2], edge_type="supersedes")
        chains[name] = ids
    related = [_insert(conn, f"[topic:rel{k}] existing note {k}") for k in range(3)]
    conn.commit()
    return {"chains": chains, "related": related}


EXPECTED_ACTIONS = {
    "confirm": ["consolidated", "created", "linked", "linked", "linked",
                "superseded", "superseded", "superseded"],
    "leaf-differs": ["consolidated", "created", "linked", "linked", "linked",
                     "superseded", "superseded", "linked"],
}

FACTS = [
    "[topic:newA] [expect:none] brand new fact one",
    "[topic:newA] [expect:none] brand new fact one, second angle",
    "[topic:newB] [expect:none] brand new fact two",
    "[topic:rel0] [expect:RELATED] another aspect of note 0",
    "[topic:rel1] [expect:RELATED] another aspect of note 1",
    "[topic:rel2] [expect:RELATED] another aspect of note 2",
    "[topic:chainA] [expect:UPDATE] chainA setting version 3",
    "[topic:chainB] [expect:UPDATE] chainB setting version 5",
    "[topic:chainC] [expect:UPDATE] chainC setting version 3",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=964)
    ap.add_argument("--d1-latency", type=float, default=0.2)
    ap.add_argument("--embed-latency", type=float, default=0.1)
    ap.add_argument("--llm-latency", type=float, default=2.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--scenario", choices=sorted(EXPECTED_ACTIONS), default="confirm")
    ap.add_argument("--compare", type=Path, help="a previous --json output; decisions must match")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="memora-roundtrips-"))
    backend = LatencyFakeD1Backend(tmp / "bench.db")
    storage.STORAGE_BACKEND = backend
    storage.EMBEDDING_MODEL = "openai"
    memora.TAG_WHITELIST = set()
    install_fakes(args.embed_latency, args.llm_latency)

    with storage.connect() as conn:
        seeded = seed(conn, args.rows, args.scenario)

    storage.invalidate_corpus_cache(storage.connect())
    LatencyFakeD1Connection.latency = args.d1_latency
    with storage.connect() as conn:
        start = time.perf_counter()
        result = storage.absorb_memory(conn, FACTS, source="bench")
        wall = time.perf_counter() - start
    LatencyFakeD1Connection.latency = 0.0

    profile = result.get("profile", {})
    decisions = [[d.get("action"), d.get("target_id"), d.get("memory_id")] for d in result["decisions"]]
    actions = [d[0] for d in decisions]
    expected = EXPECTED_ACTIONS[args.scenario]
    assert actions == expected, f"scenario {args.scenario}: actions {actions} != expected {expected}"
    # Every UPDATE landed on its chain's CURRENT leaf, never the stale target.
    leaves = {ids[-1] for ids in seeded["chains"].values()}
    for (action, target, _mid), fact in zip(decisions[-3:], FACTS[-3:]):
        assert target in leaves, f"{fact}: {action} target #{target} is not a chain leaf"
    if args.compare:
        baseline = json.loads(args.compare.read_text())["decisions"]
        assert decisions == baseline, f"decisions differ from {args.compare}:\n{decisions}\n{baseline}"
    if args.json:
        print(json.dumps({"wall_seconds": wall, "profile": profile, "decisions": decisions}, indent=2))
        return 0
    print(f"rows={args.rows} d1_latency={args.d1_latency}s embed_latency={args.embed_latency}s "
          f"llm_latency={args.llm_latency}s")
    print(f"{'phase':<20}{'requests':>10}{'seconds':>10}{'calls':>8}")
    for name, p in sorted(profile["phases"].items(), key=lambda kv: -kv[1]["seconds"]):
        print(f"{name:<20}{p['requests']:>10}{p['seconds']:>10.2f}{p['calls']:>8}")
    print(f"{'TOTAL':<20}{profile['total_requests']:>10}{profile['total_seconds']:>10.2f}")
    print("counters:", json.dumps(profile["counters"], sort_keys=True))
    print("decisions:", decisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
