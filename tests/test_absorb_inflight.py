"""Absorb process-death recovery: durable in-flight records + fail-safe reap."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

import memora
import memora.storage as storage
from memora.backends import LocalSQLiteBackend
from tests.conftest import FakeD1Backend

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
ORPHAN_FACT = "Process death orphan fact extra words for absorb"


def _spawn_absorb_sigkill(db_path: Path, *, kind: str, skip_inflight: bool = False) -> subprocess.CompletedProcess:
    """Child process runs absorb and SIGKILLs itself after the first owned INSERT.

    This is process death, not an in-process exception: the compensating
    `except` in absorb_memory never runs.
    """
    child = textwrap.dedent(
        f"""
        import os, signal, sys
        from pathlib import Path
        sys.path.insert(0, {str(REPO_ROOT)!r})
        os.environ["MEMORA_EMBEDDING_MODEL"] = "tfidf"
        os.environ["MEMORA_LLM_ENABLED"] = "false"
        os.environ["MEMORA_ALLOW_ANY_TAG"] = "1"
        import memora
        import memora.storage as storage
        memora.TAG_WHITELIST = set()
        storage.EMBEDDING_MODEL = "tfidf"
        storage.LLM_ENABLED = False
        db = Path({str(db_path)!r})
        if {kind!r} == "fake_d1":
            from tests.conftest import FakeD1Backend
            storage.STORAGE_BACKEND = FakeD1Backend(db)
        else:
            from memora.backends import LocalSQLiteBackend
            storage.STORAGE_BACKEND = LocalSQLiteBackend(db)
        if {skip_inflight!r}:
            storage._begin_absorb_inflight = lambda *a, **k: None
        def abort(_memory_id, _nonce):
            os.kill(os.getpid(), signal.SIGKILL)
        storage._after_absorb_owned_insert = abort
        conn = storage.connect()
        storage.absorb_memory(conn, [{ORPHAN_FACT!r}])
        raise SystemExit("absorb returned after SIGKILL abort — abort hook did not fire")
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["MEMORA_ALLOW_ANY_TAG"] = "1"
    env["MEMORA_EMBEDDING_MODEL"] = "tfidf"
    env["MEMORA_LLM_ENABLED"] = "false"
    return subprocess.run(
        [PYTHON, "-c", child],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _fresh_backend(db_path: Path, kind: str, monkeypatch):
    if kind == "fake_d1":
        backend = FakeD1Backend(db_path)
    else:
        backend = LocalSQLiteBackend(db_path)
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    return backend


def _nonce_rows(conn):
    return conn.execute(
        "SELECT id, metadata FROM memories "
        "WHERE metadata LIKE '%absorb_nonce%'"
    ).fetchall()


@pytest.mark.parametrize("kind", ["sqlite", "fake_d1"])
def test_process_death_orphan_detected_and_reaped(tmp_path, monkeypatch, kind):
    """SIGKILL mid-absorb: fresh connection detects the orphan, then reaps it.

    Named red ProcessDeathOrphanUndetected: skip `_begin_absorb_inflight`
    (c32b40e — nonce is in-memory only) and this assertion fails on FakeD1,
    where per-statement autocommit left the row and nothing reported it.
    """
    db_path = tmp_path / ("absorb-d1.db" if kind == "fake_d1" else "absorb.db")
    proc = _spawn_absorb_sigkill(db_path, kind=kind)
    assert proc.returncode == -9, (
        f"child must die by SIGKILL, not an exception; rc={proc.returncode} "
        f"stderr={proc.stderr[-500:]}"
    )

    _fresh_backend(db_path, kind, monkeypatch)
    conn = storage.connect()
    try:
        report = storage.list_absorb_inflight(conn)
        assert report["live"] or report["orphaned"], (
            "ProcessDeathOrphanUndetected: process-death mid-absorb left no "
            "durable in-flight record"
        )
        owned = [mid for rec in (report["live"] + report["orphaned"]) for mid in rec["owned_memory_ids"]]
        if kind == "fake_d1":
            assert owned, (
                "ProcessDeathOrphanUndetected: FakeD1 autocommit left an absorb "
                "row but list_absorb_inflight did not recover its id"
            )
            assert _nonce_rows(conn), "FakeD1 death must leave the absorb-owned row"

        future = storage._absorb_now() + timedelta(
            seconds=storage.ABSORB_INFLIGHT_LEASE_SECONDS + 5
        )
        live_reap = storage.reconcile_dead_absorbs(conn, now=storage._absorb_now())
        assert live_reap["reaped_nonces"] == [], (
            "LiveAbsorbStolen: reap with a live lease deleted in-flight work"
        )

        dead_reap = storage.reconcile_dead_absorbs(conn, now=future)
        assert dead_reap["reaped_nonces"], (
            "ProcessDeathOrphanUnreaped: expired in-flight record was not reaped"
        )
        leftover = storage.list_absorb_inflight(conn, now=future)
        assert leftover["live"] == [] and leftover["orphaned"] == []
        assert _nonce_rows(conn) == [], (
            "ProcessDeathOrphanUnreaped: absorb-owned rows survived reconciliation"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["sqlite", "fake_d1"])
def test_reconcile_does_not_steal_live_absorb(tmp_path, monkeypatch, kind):
    """Fail-safe: an unexpired lease is live even if we are a different process."""
    db_path = tmp_path / ("live-d1.db" if kind == "fake_d1" else "live.db")
    _fresh_backend(db_path, kind, monkeypatch)
    with storage.connect() as conn:
        nonce = "live-lease-nonce"
        storage._begin_absorb_inflight(conn, nonce)
        mem = storage.add_memory(
            conn,
            content="Live absorb row extra words for lease probe",
            metadata={"absorb_nonce": nonce},
        )
        result = storage.reconcile_dead_absorbs(conn)
        assert result["reaped_nonces"] == [], (
            "LiveAbsorbStolen: mutation ignore lease_until and this goes red"
        )
        assert storage.get_memory(conn, mem["id"]) is not None
        report = storage.list_absorb_inflight(conn)
        assert any(r["nonce"] == nonce for r in report["live"]), (
            "live in-flight absorb must stay visible until the lease expires"
        )


@pytest.mark.parametrize("kind", ["sqlite", "fake_d1"])
def test_reaper_cas_does_not_steal_renewed_lease(tmp_path, monkeypatch, kind):
    """Second guard: even if listing is stale, the claim UPDATE must see the live lease.

    Named red LiveAbsorbStolen: drop `AND lease_until < ?` from the reaper
    UPDATE and a heartbeat that won the race is deleted anyway.
    """
    db_path = tmp_path / ("cas-d1.db" if kind == "fake_d1" else "cas.db")
    _fresh_backend(db_path, kind, monkeypatch)
    with storage.connect() as conn:
        nonce = "renewed-lease-nonce"
        storage._begin_absorb_inflight(conn, nonce)
        mem = storage.add_memory(
            conn,
            content="Renewed absorb row extra words for cas probe",
            metadata={"absorb_nonce": nonce},
        )
        real_list = storage.list_absorb_inflight

        def stale_list(connection, *, now=None):
            recs = real_list(connection, now=now)
            return {"live": [], "orphaned": recs["live"] + recs["orphaned"]}

        monkeypatch.setattr(storage, "list_absorb_inflight", stale_list)
        result = storage.reconcile_dead_absorbs(conn)
        assert result["reaped_nonces"] == [], (
            "LiveAbsorbStolen: reaper claim ignored a still-valid lease"
        )
        assert storage.get_memory(conn, mem["id"]) is not None


@pytest.mark.parametrize("kind", ["sqlite", "fake_d1"])
def test_successful_absorb_clears_inflight(tmp_path, monkeypatch, kind):
    db_path = tmp_path / ("ok-d1.db" if kind == "fake_d1" else "ok.db")
    _fresh_backend(db_path, kind, monkeypatch)
    monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
    monkeypatch.setattr(storage, "_search_by_vector", lambda *a, **k: [])
    with storage.connect() as conn:
        result = storage.absorb_memory(conn, ["Successful absorb fact extra words"])
        assert result.get("created", 0) >= 1
        report = storage.list_absorb_inflight(conn)
        assert report["live"] == [] and report["orphaned"] == [], (
            "mutation: skip _complete_absorb_inflight and success leaves a tracking row"
        )


@pytest.mark.parametrize("kind", ["sqlite", "fake_d1"])
def test_connect_reaps_expired_inflight(tmp_path, monkeypatch, kind):
    db_path = tmp_path / ("boot-d1.db" if kind == "fake_d1" else "boot.db")
    _fresh_backend(db_path, kind, monkeypatch)
    with storage.connect() as conn:
        nonce = "expired-boot-nonce"
        storage._begin_absorb_inflight(conn, nonce)
        mem = storage.add_memory(
            conn,
            content="Expired inflight row extra words for boot reap",
            metadata={"absorb_nonce": nonce},
        )
        conn.execute(
            "UPDATE absorb_inflight SET lease_until = ? WHERE nonce = ?",
            ("2000-01-01 00:00:00", nonce),
        )
        conn.commit()
        mem_id = mem["id"]
    # Fresh connection is the boot path.
    with storage.connect() as conn:
        assert storage.get_memory(conn, mem_id) is None, (
            "mutation: skip reconcile_dead_absorbs in connect() and the orphan remains"
        )
        report = storage.list_absorb_inflight(conn)
        assert report["live"] == [] and report["orphaned"] == []


def test_health_reports_live_inflight(tmp_path, monkeypatch):
    from memora.cli import cmd_health

    _fresh_backend(tmp_path / "health.db", "sqlite", monkeypatch)
    with storage.connect() as conn:
        storage._begin_absorb_inflight(conn, "health-live-nonce")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    cmd_health()
    payload = json.loads(buf.getvalue())
    assert payload["absorb_inflight"]["live"] >= 1, (
        "mutation: drop absorb_inflight from cmd_health and this goes red"
    )
    assert payload["status"] in ("ok", "degraded")


def test_completed_inflight_is_not_deleted_as_partial(tmp_path, monkeypatch):
    """Death after status=completed must keep memory rows and only drop tracking."""
    _fresh_backend(tmp_path / "completed.db", "sqlite", monkeypatch)
    with storage.connect() as conn:
        nonce = "completed-nonce"
        storage._begin_absorb_inflight(conn, nonce)
        mem = storage.add_memory(
            conn,
            content="Completed absorb row extra words for status probe",
            metadata={"absorb_nonce": nonce},
        )
        conn.execute(
            "UPDATE absorb_inflight SET status = 'completed', "
            "lease_until = ? WHERE nonce = ?",
            ("2000-01-01 00:00:00", nonce),
        )
        conn.commit()
        result = storage.reconcile_dead_absorbs(conn)
        assert nonce in result["cleared_completed"]
        assert mem["id"] not in result["deleted_ids"]
        assert storage.get_memory(conn, mem["id"]) is not None
        assert storage.list_absorb_inflight(conn)["live"] == []
        assert storage.list_absorb_inflight(conn)["orphaned"] == []
