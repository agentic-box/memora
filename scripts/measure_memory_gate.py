#!/usr/bin/env python3
"""Memory gate for local-primary stores (local-primary plan §8 L2a).

Measures the peak RSS of ONE memora server process serving N local SQLite
stores, with FTS and the corpus cache at its budget, and derives the
container memory limit: max(768 MiB, 1.5 x peak RSS).

OFFLINE ONLY. Every store is a synthetic SQLite file in a temporary
directory; nothing connects to D1, R2 or a live store, and no embedding API
is called: the dense embedding function is replaced with a deterministic
generator of the same dimension.

Method:
  1. Build phase (one process per store, in parallel, so its memory is not
     counted): for each store, write --rows memories through memora's own add_memory (so the
     schema, FTS rows, embedding rows and meta stamps are exactly what the
     server writes), each with --content-chars of text, 3 tags, and a
     --dim-component dense vector.
  2. Measure phase (a fresh process): import memora.server (every module the
     server loads), then, for --passes passes over every store in turn:
     semantic_search (loads and caches the corpus snapshot, scores it),
     hybrid_search (FTS5 MATCH plus vector) and list_memories with a query
     (FTS5). The corpus cache budget is MEMORA_CORPUS_CACHE_BUDGET_MB
     (--budget-mb, default 384); when the stores' snapshots exceed it the
     passes also exercise LRU eviction and reload.
  3. Peak RSS is ru_maxrss of the measure process (the kernel's high-water
     mark, which includes transient peaks between samples).

  scripts/measure_memory_gate.py                          # 4 x 1000 rows, 1024-dim
  scripts/measure_memory_gate.py --rows 1500 --json       # saturate the 384 MB budget
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import resource
import subprocess
import sys
import tempfile
import time

CONTAINER_FLOOR_MB = 768
HEADROOM = 1.5
_WORDS = ("memora store replica outbox freeze journal intent export receipt seed "
          "cutover rollback snapshot vector corpus cache budget absorb consolidate "
          "project workspace leader worker review finding section plan slice "
          "deploy container volume mount health readiness token admin gate").split()


def _rss_mb_now() -> float:
    try:
        with open("/proc/self/statm") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1048576
    except OSError:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True).stdout.strip()
        return int(out) / 1024 if out else float("nan")


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return peak / 1048576 if sys.platform == "darwin" else peak / 1024


def _fake_dense(dim: int):
    def vector(text: str):
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return {str(i): x / norm for i, x in enumerate(raw)}
    return vector


def _patch_embeddings(dim: int) -> None:
    from memora import embeddings

    vector = _fake_dense(dim)
    embeddings._compute_embedding_openai = vector
    embeddings._compute_embeddings_openai_batch = lambda texts: [vector(t) for t in texts]


def _env(stores: dict, budget_mb: float) -> dict:
    env = dict(os.environ)
    for k in ("MEMORA_STORAGE_URI", "MEMORA_DB_PATH", "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN",
              "OPENAI_API_KEY", "MEMORA_EMBEDDING_API_KEY", "AWS_PROFILE"):
        env.pop(k, None)
    env.update({
        "MEMORA_DATABASES": json.dumps(stores),
        "MEMORA_DEFAULT_DB": sorted(stores)[0],
        "MEMORA_EMBEDDING_MODEL": "openai",
        "OPENAI_EMBEDDING_MODEL": "synthetic-dense",
        "MEMORA_CORPUS_CACHE_BUDGET_MB": str(budget_mb),
        "MEMORA_LLM_ENABLED": "false",
        "MEMORA_HEALTH_REFRESH_INTERVAL": "0",
        "MEMORA_ALLOW_ANY_TAG": "1",
    })
    return env


def _build(args) -> None:
    _patch_embeddings(args.dim)
    from memora import storage

    names = sorted(json.loads(os.environ["MEMORA_DATABASES"]))
    for name in ([args.only] if args.only else names):
        rng = random.Random(f"4242:{name}")
        token = storage.CURRENT_DB.set(name)
        try:
            conn = storage.connect()
            try:
                for i in range(args.rows):
                    words = []
                    while sum(len(w) + 1 for w in words) < args.content_chars:
                        words.append(rng.choice(_WORDS))
                    storage.add_memory(
                        conn,
                        content=f"{name} memory {i}: " + " ".join(words),
                        metadata={"project": name, "type": "fact", "seq": i},
                        tags=[name, f"{name}/topic{i % 17}", rng.choice(_WORDS)],
                        commit=(i % 200 == 199),
                    )
                conn.commit()
            finally:
                conn.close()
        finally:
            storage.CURRENT_DB.reset(token)


def _measure(args) -> None:
    _patch_embeddings(args.dim)
    t0 = time.time()
    import memora.server  # noqa: F401  -- the server's full import set
    from memora import storage

    samples = {"after_import": {"rss_mb": _rss_mb_now(), "peak_mb": _peak_rss_mb()}}
    names = sorted(json.loads(os.environ["MEMORA_DATABASES"]))
    conns = {}
    for n in names:
        token = storage.CURRENT_DB.set(n)
        try:
            conns[n] = storage.connect(check_same_thread=False)
        finally:
            storage.CURRENT_DB.reset(token)
    for p in range(args.passes):
        for n in names:
            token = storage.CURRENT_DB.set(n)
            try:
                conn = conns[n]
                storage.semantic_search(conn, f"{n} freeze journal intent {p}", top_k=10)
                storage.hybrid_search(conn, f"replica outbox {p}", top_k=10)
                storage.list_memories(conn, query="cutover", limit=50)
            finally:
                storage.CURRENT_DB.reset(token)
        samples[f"pass_{p + 1}"] = {"rss_mb": _rss_mb_now(), "peak_mb": _peak_rss_mb()}
    with storage._corpus_cache_lock:
        cache = {k: round(e.nbytes / 1048576, 1) for k, e in storage._corpus_cache.items()}
    print(json.dumps({
        "samples": samples,
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "corpus_cache_estimated_mb": cache,
        "seconds": round(time.time() - t0, 1),
    }))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stores", type=int, default=4)
    ap.add_argument("--rows", type=int, default=1000, help="memories per store (live: 964+)")
    ap.add_argument("--dim", type=int, default=1024, help="dense embedding components")
    ap.add_argument("--content-chars", type=int, default=600)
    ap.add_argument("--budget-mb", type=float, default=384)
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--phase", choices=["build", "measure"], help=argparse.SUPPRESS)
    ap.add_argument("--only", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.phase == "build":
        _build(args)
        return 0
    if args.phase == "measure":
        _measure(args)
        return 0

    passthrough = [f"--rows={args.rows}", f"--dim={args.dim}",
                   f"--content-chars={args.content_chars}", f"--passes={args.passes}"]
    with tempfile.TemporaryDirectory(prefix="memora-memgate-") as tmp:
        stores = {f"s{i}": os.path.join(tmp, f"s{i}.db") for i in range(args.stores)}
        env = _env(stores, args.budget_mb)
        # One build process per store, in parallel: add_memory's per-insert
        # work grows with the store, so a serial build is the slow part.
        builds = [subprocess.Popen([sys.executable, __file__, "--phase=build", f"--only={n}",
                                    *passthrough], env=env) for n in stores]
        if any(p.wait() != 0 for p in builds):
            raise SystemExit("fixture build failed")
        sizes = {n: round(os.path.getsize(p) / 1048576, 1) for n, p in stores.items()}
        out = subprocess.run([sys.executable, __file__, "--phase=measure", *passthrough],
                             env=env, check=True, capture_output=True, text=True).stdout
    result = json.loads(out.strip().splitlines()[-1])
    peak = result["peak_rss_mb"]
    limit = max(CONTAINER_FLOOR_MB, math.ceil(HEADROOM * peak))
    result.update({
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "stores": args.stores, "rows_per_store": args.rows, "dim": args.dim,
        "content_chars": args.content_chars, "budget_mb": args.budget_mb,
        "store_file_mb": sizes,
        "limit_mb": limit,
        "fits_768": peak * HEADROOM <= CONTAINER_FLOOR_MB,
    })
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{args.stores} stores x {args.rows} rows, {args.dim}-dim, budget {args.budget_mb} MB "
              f"({sys.platform}, python {result['python']})")
        for k, v in result["samples"].items():
            print(f"  {k:<14} rss {v['rss_mb']:7.1f} MB   peak {v['peak_mb']:7.1f} MB")
        print(f"  corpus cache (estimated): {result['corpus_cache_estimated_mb']}")
        print(f"  peak RSS {peak} MB -> limit max({CONTAINER_FLOOR_MB}, {HEADROOM} x peak) = {limit} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
