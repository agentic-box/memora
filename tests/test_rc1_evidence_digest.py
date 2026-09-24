"""RC1 (leader 8063): the reconcile evidence digest covers the evidence's
CONTENT, not the time it was read. Production: bestation intent 53 could
never be accepted -- every GET /admin/intents re-gathers the evidence with a
new read_at, so the digest the operator quoted never matched."""
from __future__ import annotations

import json

import pytest

from memora import admin, reconcile, storage
from memora.local_primary import L5Refused, reconcile_accept
from tests.test_l2_sync_admin import _open_intent, _Reader, registry  # noqa: F401 (fixture)

ROWS = [{"key": "embedding_rebuild_lease", "value": "owner-1"}]


@pytest.fixture
def read_now(monkeypatch):
    """The read-back bound passed: evidence is READ (status "read")."""
    monkeypatch.setattr(reconcile, "RECONCILE_MIN_AGE_S", 0)


def _receipt(tmp_path):
    good = tmp_path / "receipt.json"
    good.write_text(json.dumps({"db": "remote", "verified_at": "2026-09-24T00:00:00Z"}))
    return str(good)


def test_two_gets_give_the_same_digest_when_d1_is_unchanged(registry, read_now):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    reader = _Reader([(ROWS, {"served_by_primary": True})])
    first = admin.list_intents("remote", reader=reader)[1]["open_intents"][0]
    second = admin.list_intents("remote", reader=reader)[1]["open_intents"][0]
    assert first["evidence"]["status"] == "read"
    assert first["evidence_sha256"] == second["evidence_sha256"]


def test_accept_with_the_shown_digest_succeeds_after_another_get(registry, read_now, tmp_path):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    reader = _Reader([(ROWS, {"served_by_primary": True})])
    shown = admin.list_intents("remote", reader=reader)[1]["open_intents"][0]["evidence_sha256"]
    admin.list_intents("remote", reader=reader)  # the CLI's own GET right before its POST
    status, body = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                       decision="applied", evidence_digest=shown, reader=reader)
    assert status == 200 and body["open_intents"] == []


def test_a_real_d1_change_between_show_and_accept_is_refused(registry, read_now, tmp_path):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    shown = admin.list_intents("remote", reader=_Reader([(ROWS, {"served_by_primary": True})]))[1]
    digest = shown["open_intents"][0]["evidence_sha256"]
    changed = _Reader([([{"key": "embedding_rebuild_lease", "value": "owner-2"}], {"served_by_primary": True})])
    admin.list_intents("remote", reader=changed)
    status, body = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                       decision="applied", evidence_digest=digest, reader=changed)
    assert status == 409 and body["error"] == "evidence_changed"
    assert storage.backend_for("remote").journal().status()[0] == [1]


@pytest.mark.parametrize("field, a, b", [
    ("rows", ROWS, []), ("row_count", 1, 0), ("served_by_primary", True, False),
    ("query", "SELECT 1", "SELECT 2"), ("status", "read", "error"),
])
def test_every_content_field_is_covered(field, a, b):
    base = {"status": "read", "query": "SELECT 1", "rows": ROWS, "row_count": 1, "served_by_primary": True,
            "read_at": 1.0}
    assert admin.evidence_sha256({**base, field: a}) != admin.evidence_sha256({**base, field: b})


def test_timing_fields_are_not_covered():
    base = {"status": "read", "query": "q", "rows": [], "row_count": 0, "served_by_primary": True}
    assert admin.evidence_sha256({**base, "read_at": 1.0}) == admin.evidence_sha256({**base, "read_at": 99.0})
    wait = {"status": "waiting"}
    assert admin.evidence_sha256({**wait, "eligible_in_s": 50.0}) == admin.evidence_sha256({**wait, "eligible_in_s": 3.2})


def test_the_cli_accept_path_matches_across_its_own_get(registry, read_now, tmp_path, monkeypatch):
    """local_primary.reconcile_accept re-GETs before posting: with the content
    digest, the operator's quoted digest matches that fresh GET."""
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    reader = _Reader([(ROWS, {"served_by_primary": True})])
    digest = admin.list_intents("remote", reader=reader)[1]["open_intents"][0]["evidence_sha256"]
    monkeypatch.setattr("memora.local_primary.load_receipt", lambda *a, **k: {})

    class Client:
        def intents(self):
            return admin.list_intents("remote", reader=reader)[1]

        def accept(self, iid, body):
            return admin.accept_intent("remote", iid, body["receipt"], operator=body["operator"],
                                       body_intent_id=body["intent_id"], decision=body["decision"],
                                       evidence_digest=body["evidence_sha256"], reader=reader)

    out = reconcile_accept("remote", Client(), intent_id=1, receipt_path=_receipt(tmp_path), operator="spok",
                           decision="applied", evidence_sha256=digest, account_id="acct", database_id="db1")
    assert out["response"][0] == 200
    with pytest.raises(L5Refused, match="not open"):
        reconcile_accept("remote", Client(), intent_id=1, receipt_path=_receipt(tmp_path), operator="spok",
                         decision="applied", evidence_sha256=digest, account_id="acct", database_id="db1")


def test_a_direct_post_after_a_d1_change_is_refused_without_another_get(registry, read_now, tmp_path):
    """Review 8067: the accept reads D1 itself; a stale cache from the last
    GET cannot resolve an intent whose evidence has since changed."""
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    digest = admin.list_intents("remote", reader=_Reader([(ROWS, {"served_by_primary": True})]))[1][
        "open_intents"][0]["evidence_sha256"]
    now_d1 = _Reader([([{"key": "embedding_rebuild_lease", "value": "owner-2"}], {"served_by_primary": True})])
    status, body = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                       decision="applied", evidence_digest=digest, reader=now_d1)
    assert status == 409 and body["error"] == "evidence_changed"
    assert now_d1.calls, "the accept read D1"
    assert storage.backend_for("remote").journal().status()[0] == [1]


class _Broken:
    def execute(self, sql, params=None):
        raise ConnectionError("D1 unreachable")


def test_a_failed_read_at_accept_time_is_refused(registry, read_now, tmp_path):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    digest = admin.list_intents("remote", reader=_Reader([(ROWS, {"served_by_primary": True})]))[1][
        "open_intents"][0]["evidence_sha256"]
    status, body = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                       decision="applied", evidence_digest=digest, reader=_Broken())
    assert status == 409 and body["error"] == "evidence_unusable"
    assert storage.backend_for("remote").journal().status()[0] == [1]


def test_the_same_error_on_get_and_post_is_refused(registry, read_now, tmp_path):
    """Review 8075: an error shown by the GET and repeated at the POST has the
    same digest -- it must still refuse, since no D1 read succeeded."""
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    shown = admin.list_intents("remote", reader=_Broken())[1]["open_intents"][0]
    assert shown["evidence"]["status"] == "error"
    status, body = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                       decision="applied", evidence_digest=shown["evidence_sha256"],
                                       reader=_Broken())
    assert status == 409 and body["error"] == "evidence_unusable" and body["status"] == "error"
    assert storage.backend_for("remote").journal().status()[0] == [1]


def test_a_read_never_served_by_the_primary_is_refused(registry, read_now, tmp_path, monkeypatch):
    monkeypatch.setattr(reconcile, "PRIMARY_RETRY_DELAY_S", 0)
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    replica = _Reader([(ROWS, {"served_by_primary": False})])
    shown = admin.list_intents("remote", reader=replica)[1]["open_intents"][0]
    assert shown["evidence"]["served_by_primary"] is False
    status, body = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                       decision="applied", evidence_digest=shown["evidence_sha256"],
                                       reader=replica)
    assert status == 409 and body["error"] == "evidence_unusable"
    assert storage.backend_for("remote").journal().status()[0] == [1]


def test_no_evidence_intents_stay_acceptable_on_the_receipt(registry, read_now, tmp_path):
    """No query can be derived (a DELETE with no key): accepted as before."""
    _open_intent(registry, "DELETE FROM memories", ())
    shown = admin.list_intents("remote", reader=_Broken())[1]["open_intents"][0]
    assert shown["evidence"]["status"] == "no-evidence"
    status, _ = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                    decision="not-applied", evidence_digest=shown["evidence_sha256"],
                                    reader=_Broken())
    assert status == 200


def test_a_direct_post_with_d1_unchanged_is_accepted(registry, read_now, tmp_path):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    reader = _Reader([(ROWS, {"served_by_primary": True})])
    digest = admin.list_intents("remote", reader=reader)[1]["open_intents"][0]["evidence_sha256"]
    status, _ = admin.accept_intent("remote", 1, _receipt(tmp_path), operator="spok", body_intent_id=1,
                                    decision="applied", evidence_digest=digest, reader=reader)
    assert status == 200
