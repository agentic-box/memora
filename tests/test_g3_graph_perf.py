"""G3a: the graph's cluster pass (storage._build_similarity_graph) gives
the same adjacency, bit for bit, as the original all-pairs
embeddings.cosine_similarity loop, without its per-pair costs."""
from __future__ import annotations

import random

import pytest

from memora import embeddings, storage


def _reference(vectors, ids, min_score):
    adj = {mid: {} for mid in ids}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            score = embeddings.cosine_similarity(vectors[a], vectors[b])
            if score >= min_score:
                adj[a][b] = score
                adj[b][a] = score
    return adj


def _dense(rnd, dim, topic):
    v = [rnd.gauss(0, 1) + (2.5 if k % 7 == topic else 0.0) for k in range(dim)]
    return {str(k): x for k, x in enumerate(v)}


def _sparse(rnd):
    return {f"w{rnd.randrange(40)}": rnd.random() for _ in range(rnd.randrange(1, 12))}


@pytest.mark.parametrize("kind", ["dense", "sparse", "mixed"])
@pytest.mark.parametrize("min_score", [0.0, 0.3, 0.9])
def test_the_adjacency_is_identical_to_the_all_pairs_reference(monkeypatch, kind, min_score):
    rnd = random.Random(hash((kind, min_score)) & 0xFFFF)
    vectors = {}
    for mid in range(1, 61):
        if kind == "dense" or (kind == "mixed" and mid % 3 == 0):
            vectors[mid] = _dense(rnd, 64, mid % 7)
        elif kind == "mixed" and mid % 3 == 1:
            vectors[mid] = {}  # an empty vector scores 0.0 against everything
        else:
            vectors[mid] = _sparse(rnd)
    if kind == "mixed":
        vectors[61] = dict(reversed(list(_dense(rnd, 64, 1).items())))  # same keys, another order
        vectors[62] = {str(k): 0.0 for k in range(64)}  # a zero-norm vector
    ids = sorted(vectors)
    monkeypatch.setattr(storage, "_get_embeddings_for_ids", lambda conn, mids: {m: vectors[m] for m in mids})
    got = storage._build_similarity_graph(None, ids, min_score)
    assert got == _reference(vectors, ids, min_score)  # same keys, same float values, bit for bit


class _NoLookup(dict):
    """A dense vector the fast path must read only by iteration: the dict
    loop's per-key .get() raises."""

    def get(self, *a, **k):
        raise AssertionError("dense vectors must be dotted as float lists, not looked up key by key")


def test_dense_vectors_never_take_the_per_pair_dict_loop(monkeypatch):
    rnd = random.Random(3)
    vectors = {mid: _NoLookup(_dense(rnd, 32, mid % 5)) for mid in range(1, 41)}
    monkeypatch.setattr(storage, "_get_embeddings_for_ids", lambda conn, mids: {m: vectors[m] for m in mids})
    calls = []
    monkeypatch.setattr(storage, "_cosine_similarity", lambda a, b: calls.append(1) or 0.0)
    got = storage._build_similarity_graph(None, sorted(vectors), 0.3)
    assert calls == [], "no per-pair cosine_similarity (and no per-pair norms)"
    assert got == _reference({m: dict(v) for m, v in vectors.items()}, sorted(vectors), 0.3)
