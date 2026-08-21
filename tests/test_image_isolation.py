"""memora #965 phase 3: R2 image keys must be database-namespaced.

Codex P0 on 42bf104. Image keys were images/{memory_id}/..., with no database
in them. Row ids are small integers and EVERY store uses them, so alpha memory
1 and beta memory 1 shared one external prefix — and delete_memory_images()
deletes a whole prefix, so deleting alpha's memory 1 destroyed beta's images.

This is the leak class the routing mutation cannot catch by construction: the
request is correctly routed, and the key is derived from the row id alone.
"""
import pytest

from memora import image_storage
from memora.storage import CURRENT_DB


class _FakeS3:
    """Records keys; models put/list/delete well enough for prefix semantics."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket=None, Key=None, Body=None, **kw):
        self.objects[Key] = Body
        return {}

    def delete_object(self, Bucket=None, Key=None, **kw):
        self.objects.pop(Key, None)
        return {}

    def delete_objects(self, Bucket=None, Delete=None, **kw):
        for obj in Delete.get("Objects", []):
            self.objects.pop(obj["Key"], None)
        return {}

    def get_paginator(self, _name):
        outer = self

        class _P:
            def paginate(self, Bucket=None, Prefix="", **kw):
                keys = [k for k in outer.objects if k.startswith(Prefix)]
                yield {"Contents": [{"Key": k} for k in keys]} if keys else {}

        return _P()


@pytest.fixture
def store(monkeypatch):
    s = image_storage.R2ImageStorage.__new__(image_storage.R2ImageStorage)
    s.bucket = "test"
    s.public_domain = None
    s.endpoint_url = None
    s.s3_client = _FakeS3()
    return s


def _bind(name):
    class _Ctx:
        def __enter__(self):
            self.token = CURRENT_DB.set(name)

        def __exit__(self, *exc):
            CURRENT_DB.reset(self.token)
    return _Ctx()


def test_same_memory_id_in_two_databases_gets_distinct_keys(store):
    with _bind("alpha"):
        a = store._generate_key(1, 0, "a" * 64, "png")
    with _bind("beta"):
        b = store._generate_key(1, 0, "a" * 64, "png")
    assert a != b, f"memory 1 in both databases shares one object key: {a}"
    assert "alpha" in a and "beta" in b


def test_deleting_one_database_does_not_delete_the_others_images(store):
    with _bind("alpha"):
        store.s3_client.put_object(Bucket="test", Key=store._generate_key(1, 0, "x" * 64, "png"), Body=b"A")
    with _bind("beta"):
        store.s3_client.put_object(Bucket="test", Key=store._generate_key(1, 0, "y" * 64, "png"), Body=b"B")
    assert len(store.s3_client.objects) == 2

    with _bind("alpha"):
        store.delete_memory_images(1)

    survivors = list(store.s3_client.objects)
    assert len(survivors) == 1, f"alpha's delete destroyed beta's images: {survivors}"
    assert "beta" in survivors[0]


def test_unbound_keeps_the_legacy_key_shape(store):
    """Existing single-database deployments must keep reachable objects."""
    key = store._generate_key(7, 0, "z" * 64, "png")
    assert key.startswith("images/7/"), (
        f"legacy key shape changed to {key!r}; already-uploaded images would be "
        "orphaned"
    )


def test_unbound_delete_does_not_reach_a_namespaced_object(store):
    with _bind("alpha"):
        store.s3_client.put_object(Bucket="test", Key=store._generate_key(1, 0, "x" * 64, "png"), Body=b"A")
    legacy = "images/1/legacy_object.png"
    store.s3_client.put_object(Bucket="test", Key=legacy, Body=b"L")

    store.delete_memory_images(1)          # unbound: legacy prefix only

    remaining = list(store.s3_client.objects)
    assert legacy not in remaining, "legacy delete failed"
    assert len(remaining) == 1 and "alpha" in remaining[0], (
        f"unbound delete reached a namespaced object: {remaining}"
    )
