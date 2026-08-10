"""Minimal CLI for clmux daemon integration.

Usage:
    python3 -m memora.cli search "query" [--top-k 7] [--tags tag1,tag2]
    python3 -m memora.cli health
    echo "fact" | python3 -m memora.cli absorb [--source S] [--context C] [--tags t1,t2]

Env loading: handled by `memora/__init__.py` _bootstrap_mcp_env() which
walks up from cwd to find .mcp.json and exports the memora server env
(MEMORA_STORAGE_URI, MEMORA_ALLOW_ANY_TAG, CLOUDFLARE_API_TOKEN, etc.)
before TAG_WHITELIST and other module-level state are computed.
"""
from __future__ import annotations

import json
import sys


def cmd_health() -> None:
    from .storage import connect, get_statistics

    try:
        conn = connect()
        try:
            stats = get_statistics(conn)
            count = stats.get("total_memories", 0)
        finally:
            conn.close()
        json.dump({"status": "ok", "memory_count": count}, sys.stdout)
    except Exception as exc:
        json.dump({"status": "error", "message": str(exc)}, sys.stdout)
        sys.exit(1)


def cmd_search(query: str, top_k: int = 7, tags_any: list[str] | None = None) -> None:
    from .storage import connect, hybrid_search
    from .embeddings import EmbeddingIntegrityFault, embedding_integrity_fault_payload

    conn = connect()
    try:
        try:
            results = hybrid_search(
                conn, query, semantic_weight=0.6, top_k=top_k,
                min_score=0.0, tags_any=tags_any or None,
            )
        except EmbeddingIntegrityFault as exc:
            json.dump(embedding_integrity_fault_payload(exc), sys.stdout)
            return
    finally:
        conn.close()

    # Compact preview: strip full content, keep preview
    for entry in results:
        if "memory" in entry:
            mem = entry["memory"]
            full = mem.pop("content", "") or ""
            mem["content_preview"] = full[:300] + "\u2026" if len(full) > 300 else full

    json.dump({"count": len(results), "results": results}, sys.stdout)


def cmd_absorb(
    fact: str,
    source: str = "manual",
    context: str | None = None,
    tags: list[str] | None = None,
) -> None:
    from .storage import absorb_memory, connect

    conn = connect()
    try:
        result = absorb_memory(
            conn,
            [fact],
            source=source,
            context=context,
            tags=tags or None,
        )
    finally:
        conn.close()

    json.dump(result, sys.stdout, default=str)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("usage: python3 -m memora.cli {health|search|absorb} ...", file=sys.stderr)
        sys.exit(1)

    cmd = args[0]
    if cmd == "health":
        cmd_health()
    elif cmd == "search":
        if len(args) < 2:
            print("usage: python3 -m memora.cli search QUERY [--top-k N] [--tags t1,t2]", file=sys.stderr)
            sys.exit(1)
        query = args[1]
        top_k = 7
        tags_any = None
        i = 2
        while i < len(args):
            if args[i] == "--top-k" and i + 1 < len(args):
                top_k = int(args[i + 1])
                i += 2
            elif args[i] == "--tags" and i + 1 < len(args):
                tags_any = args[i + 1].split(",")
                i += 2
            else:
                i += 1
        cmd_search(query, top_k=top_k, tags_any=tags_any)
    elif cmd == "absorb":
        # Read fact from stdin (avoids argv length limits for large facts)
        fact = sys.stdin.read().strip()
        if not fact:
            print("error: absorb expects a non-empty fact on stdin", file=sys.stderr)
            sys.exit(1)
        source = "manual"
        context: str | None = None
        tags: list[str] | None = None
        i = 1
        while i < len(args):
            if args[i] == "--source" and i + 1 < len(args):
                source = args[i + 1]
                i += 2
            elif args[i] == "--context" and i + 1 < len(args):
                context = args[i + 1]
                i += 2
            elif args[i] == "--tags" and i + 1 < len(args):
                tags = [t for t in args[i + 1].split(",") if t]
                i += 2
            else:
                i += 1
        cmd_absorb(fact, source=source, context=context, tags=tags)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
