"""Admin endpoints for the write gate and the D1 intent journal.

docs/local-primary-implementation.md §1:
  POST   /admin/freeze/{name}              freeze (drain, then persist the intent)
  DELETE /admin/freeze/{name}              lift the freeze
  GET    /admin/intents/{name}             open D1 write intents + read-back evidence
  POST   /admin/reconcile/{name}/{id}      operator accepts one open intent (needs a receipt)

Authorisation is NOT defined here. `require_admin(request)` is an injectable
hook: the admin auth layer (MEMORA_ADMIN_TOKEN, loopback binding; slice L2a,
plan §9 item a) installs it with set_admin_auth(). Until then the
placeholder refuses every request, so these routes can be registered safely
either way.

The handlers below are plain functions returning (status, body) so they are
testable without HTTP; register_admin_routes wraps them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .write_gate import FreezeTimeout, _readonly_names, persist_freeze, remove_freeze

logger = logging.getLogger(__name__)

Result = Tuple[int, Dict[str, Any]]


def _refuse_by_default(request) -> Optional[Result]:
    return 403, {"error": "admin_auth_not_configured"}


_admin_auth: Callable[[Any], Optional[Result]] = _refuse_by_default


def set_admin_auth(fn: Callable[[Any], Optional[Result]]) -> None:
    """Install the admin auth check: fn(request) -> None when authorised,
    else (status, body) to return."""
    global _admin_auth
    _admin_auth = fn


def require_admin(request) -> Optional[Result]:
    return _admin_auth(request)


def _backend(name: str):
    from .storage import DatabaseRegistryError, backend_for

    try:
        return backend_for(name), None
    except DatabaseRegistryError as exc:
        return None, (404, {"error": "unknown_database", "message": str(exc)})


def freeze_store(name: str, timeout_s: float = 30.0) -> Result:
    backend, err = _backend(name)
    if err:
        return err
    if not hasattr(backend, "write_gate"):
        return 400, {"error": "store_has_no_write_gate"}
    gate = backend.write_gate()
    try:
        gate.freeze(timeout_s=timeout_s)
    except FreezeTimeout as exc:
        return 409, {"state": gate.state, "error": "freeze_timeout", "in_flight": exc.in_flight}
    try:
        persist_freeze(name)
    except OSError as exc:
        # Frozen in memory (writes refused) but not persisted: say so.
        logger.error("freeze of %s not persisted: %s", name, exc)
        return 500, {**gate.status(), "error": "freeze_not_persisted", "message": str(exc)}
    return 200, gate.status()


def thaw_store(name: str) -> Result:
    backend, err = _backend(name)
    if err:
        return err
    if not hasattr(backend, "write_gate"):
        return 400, {"error": "store_has_no_write_gate"}
    if name in _readonly_names():
        return 409, {"error": "configured_read_only", "message": "named in MEMORA_READONLY_DBS"}
    remove_freeze(name)
    gate = backend.write_gate()
    gate.thaw()
    _sweep_pending_images(backend)
    return 200, gate.status()


def _sweep_pending_images(backend) -> None:
    """Deferred images cannot be applied while a store is frozen; retry them
    on thaw (L4 review 7610 P2, §9 s). Local stores only; never raises."""
    from .backends import LocalSQLiteBackend

    if not isinstance(backend, LocalSQLiteBackend):
        return
    try:
        from .storage import sweep_pending_images

        conn = backend.connect()
        try:
            sweep_pending_images(conn)
        finally:
            conn.close()
    except Exception as exc:
        logger.error("pending-image sweep after thaw failed: %s", exc)


def list_intents(name: str, *, reader=None) -> Result:
    backend, err = _backend(name)
    if err:
        return err
    if not hasattr(backend, "journal"):
        return 400, {"error": "not_a_d1_store"}
    from .reconcile import gather_evidence

    journal = backend.journal()
    evidence = gather_evidence(backend, journal, reader=reader)
    ids, broken = journal.status()
    return 200, {
        "state": backend.write_gate().state,
        "journal_error": broken,
        "open_intents": [
            {**{k: rec.get(k) for k in ("id", "sql", "target", "keys", "post_state", "sent_at", "params_sha256")},
             "evidence": evidence.get(int(rec["id"])),
             "evidence_sha256": evidence_sha256(evidence.get(int(rec["id"])))}
            for rec in journal.open_intents()
        ],
    }


def _load_receipt(name: str, receipt: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Result]]:
    if not isinstance(receipt, str) or not receipt:
        return None, (400, {"error": "receipt_required"})
    path = Path(receipt)
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        return None, (400, {"error": "receipt_unreadable", "message": str(exc)[:200]})
    if not isinstance(data, dict) or data.get("db") != name or not data.get("verified_at"):
        return None, (400, {"error": "receipt_invalid",
                            "message": "the receipt must be a verified export receipt for this database"})
    return {"receipt": str(path), "receipt_sha256": hashlib.sha256(raw).hexdigest()}, None


DECISIONS = ("applied", "not-applied")


# The evidence digest covers what was READ, never when (RC1): the gathering
# rule's version, the status, the query, the rows, the row count and
# served_by_primary. read_at and a waiting intent's eligible_in_s change on
# every gather -- including them made the digest change on every GET, so no
# operator decision could ever match (beta intent 53). Evidence is
# gathered on every GET, and accept_intent gathers it AGAIN (reads D1) at
# POST time: the decision is judged against D1 as it is when accepted, so a
# real change between show and accept is refused even on a direct POST.
EVIDENCE_RULE = 1
_EVIDENCE_TIMING_KEYS = ("read_at", "eligible_in_s")


def evidence_sha256(evidence: Any) -> str:
    """The digest an operator quotes to prove which evidence they decided on
    (GET /admin/intents returns it per intent): the evidence CONTENT only."""
    content = ({k: v for k, v in evidence.items() if k not in _EVIDENCE_TIMING_KEYS}
               if isinstance(evidence, dict) else evidence)
    payload = json.dumps({"rule": EVIDENCE_RULE, "evidence": content}, sort_keys=True, default=str,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def accept_intent(name: str, intent_id: int, receipt: Any, *, operator: Any = None,
                  body_intent_id: Any = None, decision: Any = None, evidence_digest: Any = None,
                  reader=None) -> Result:
    """Operator acceptance of one open intent. Required (review 7584 P2): a
    verified export receipt for this database, the operator, the intent id
    (must match the path), the decision (applied | not-applied) and the
    sha256 of the evidence the operator saw (must match the current one)."""
    backend, err = _backend(name)
    if err:
        return err
    if not hasattr(backend, "journal"):
        return 400, {"error": "not_a_d1_store"}
    extra, err = _load_receipt(name, receipt)
    if err:
        return err
    if not isinstance(operator, str) or not operator.strip():
        return 400, {"error": "operator_required"}
    if body_intent_id != intent_id:
        return 400, {"error": "intent_id_mismatch", "message": "the body's intent_id must equal the path's"}
    if decision not in DECISIONS:
        return 400, {"error": "decision_required", "allowed": list(DECISIONS)}
    journal = backend.journal()
    if intent_id not in journal.status()[0]:
        return 404, {"error": "no_such_open_intent", "id": intent_id}
    if journal.evidence.get(intent_id) is None:
        return 409, {"error": "no_evidence_yet", "message": "GET /admin/intents first, and decide on its evidence"}
    # Re-read D1 NOW (review 8067): the quoted digest must match the evidence
    # as it is at accept time, not the cache of the last GET. A failed read
    # gives error evidence, whose digest cannot match: refused.
    from .reconcile import gather_evidence

    current = gather_evidence(backend, journal, reader=reader).get(intent_id)
    if current is None:
        return 409, {"error": "evidence_changed", "message": "the evidence is not the one this decision quotes"}
    # Fail closed on a read that did not succeed (review 8075): an error, or
    # a read never served by D1's primary, proves nothing -- even when the GET
    # showed the same. Accepted without a D1 read, as before and explicitly:
    # "no-evidence" (no query can be derived from the statement; the decision
    # rests on the receipt) and "waiting" (before the read-back bound).
    status = current.get("status")
    if status == "error" or (status == "read" and current.get("served_by_primary") is not True):
        return 409, {"error": "evidence_unusable", "status": status,
                     "message": "D1 could not be read from its primary now; retry the accept later"}
    if evidence_digest != evidence_sha256(current):
        return 409, {"error": "evidence_changed", "message": "the evidence is not the one this decision quotes"}
    extra = {**extra, "operator": operator.strip(), "decision": decision,
             "evidence_sha256": evidence_digest}
    if not journal.resolve(intent_id, "operator-accepted", durable=True, extra=extra):
        return 500, {"error": "resolution_not_recorded", "id": intent_id}
    return 200, {"id": intent_id, "outcome": "operator-accepted", **backend.write_gate().status()}


def compare_action(name: str, action: str, body: Any) -> Result:
    """POST /admin/compare/<name>/<begin|record|abort> (plan §5.2, L6).

    Operator ATTESTATION of a verified report, not a trusted assertion
    (review 7695): the admin token is the operator; the server verifies
    what it can itself. `begin` registers a run (its start by this server's
    clock); `record` takes the report FILE path -- readable here, like a
    reconcile receipt -- and the replicator module verifies the file's hash,
    the store identity, the registered run and its age, the snapshot's hash,
    and derives compare_consumed_seq from the report's H (clean barrier or
    nightly only, never past the acked head). `abort` drops a run."""
    from .backends import LocalSQLiteBackend
    from .replicator import CompareNotRecorded, abort_compare_for, begin_compare_for, record_compare_for

    backend, err = _backend(name)
    if err:
        return err
    if not isinstance(backend, LocalSQLiteBackend):
        return 400, {"error": "not_a_local_store"}
    if not isinstance(body, dict):
        return 400, {"error": "bad_request"}
    try:
        if action == "begin":
            return 200, begin_compare_for(backend)
        if action == "abort":
            if not isinstance(body.get("run_id"), str):
                return 400, {"error": "run_id_required"}
            abort_compare_for(backend, body["run_id"])
            return 200, {"aborted": body["run_id"]}
        if action == "record":
            return 200, record_compare_for(backend, db=name, report_path=body.get("report"),
                                           report_sha256=body.get("report_sha256"))
    except CompareNotRecorded as exc:
        return 409, {"error": "compare_not_recorded", "message": str(exc)}
    except Exception as exc:  # e.g. no sync_state: replication is not installed on this store
        return 409, {"error": "compare_not_recorded", "message": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return 404, {"error": "unknown_action", "allowed": ["begin", "record", "abort"]}


def gate_health(name: str) -> Optional[Dict[str, Any]]:
    """freeze/journal fields for /health/db (live, never raises, no D1 call)."""
    try:
        backend, err = _backend(name)
        if err or not hasattr(backend, "write_gate"):
            return None
        out: Dict[str, Any] = {"freeze": backend.write_gate().status()}
        if hasattr(backend, "journal"):
            j = backend.journal()
            out["journal"] = {"path": str(j.path), "evidence": {str(k): v.get("status") for k, v in j.evidence.items()}}
            ids, broken = j.status()
            if broken:
                out["journal"]["unusable"] = broken  # this process serves the store read-only
        if getattr(backend, "refused_reason", None):
            out["refused"] = backend.refused_reason
        from .shadow import shadow_status

        try:
            sh = shadow_status(name)
        except Exception as exc:  # the freeze/journal fields must still be reported
            sh = {"enabled": True, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        if sh is not None:
            out["shadow"] = sh
        from .storage import embedding_repair_status

        repair = embedding_repair_status(name)
        if repair is not None:
            out.setdefault("embeddings", {})["repair_needed"] = repair  # E1: repaired only by an explicit action
        from .storage import embedding_model_unrecorded_status

        unrecorded = embedding_model_unrecorded_status(name)
        if unrecorded is not None:
            out.setdefault("embeddings", {})["model_unrecorded"] = unrecorded  # E1b: served, not yet recorded
        from .replicator import replicator_for, start_refusal

        rep = replicator_for(name)
        if rep is not None:
            out["replication"] = rep.status()
        elif start_refusal(name):
            # configured but not started (e.g. an invalid timing value): say why
            out["replication"] = {"status": "refused", "error": start_refusal(name)}
        return out
    except Exception as exc:
        return {"freeze": {"state": "unknown", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}}


def register_admin_routes(mcp: Any) -> None:
    from starlette.responses import JSONResponse

    def respond(result: Result):
        status, body = result
        return JSONResponse(body, status_code=status)

    async def _run(fn, *args, **kwargs):
        import anyio

        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    @mcp.custom_route("/admin/freeze/{name}", methods=["POST", "DELETE"])
    async def _freeze(request):
        denied = require_admin(request)
        if denied is not None:
            return respond(denied)
        name = request.path_params["name"]
        if request.method == "DELETE":
            return respond(await _run(thaw_store, name))
        try:
            timeout_s = float(request.query_params.get("timeout_s", "30"))
        except ValueError:
            return respond((400, {"error": "bad_timeout"}))
        return respond(await _run(freeze_store, name, max(0.0, min(timeout_s, 300.0))))

    @mcp.custom_route("/admin/compare/{name}/{action}", methods=["POST"])
    async def _compare(request):
        denied = require_admin(request)
        if denied is not None:
            return respond(denied)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return respond((400, {"error": "bad_request"}))
        return respond(await _run(compare_action, request.path_params["name"],
                                  request.path_params["action"], body))

    @mcp.custom_route("/admin/intents/{name}", methods=["GET"])
    async def _intents(request):
        denied = require_admin(request)
        if denied is not None:
            return respond(denied)
        return respond(await _run(list_intents, request.path_params["name"]))

    @mcp.custom_route("/admin/reconcile/{name}/{intent_id}", methods=["POST"])
    async def _reconcile(request):
        denied = require_admin(request)
        if denied is not None:
            return respond(denied)
        try:
            intent_id = int(request.path_params["intent_id"])
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return respond((400, {"error": "bad_request"}))
        if not isinstance(body, dict):
            return respond((400, {"error": "bad_request"}))
        return respond(await _run(
            accept_intent, request.path_params["name"], intent_id, body.get("receipt"),
            operator=body.get("operator"), body_intent_id=body.get("intent_id"),
            decision=body.get("decision"), evidence_digest=body.get("evidence_sha256"),
        ))
