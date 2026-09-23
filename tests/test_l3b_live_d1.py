"""Live checks against a THROWAWAY Cloudflare D1 database (plan §2.9, §4, §6.1).

SKIPPED unless every MEMORA_D1_TEST_* variable is set. They must name a
database created only for this purpose: the module refuses to run unless
MEMORA_D1_TEST_DATABASE_NAME contains "throwaway", and it never touches any
other database. Nothing here runs in CI.

  MEMORA_D1_TEST_ACCOUNT        Cloudflare account id
  MEMORA_D1_TEST_DATABASE       the throwaway database's id
  MEMORA_D1_TEST_DATABASE_NAME  its name (must contain "throwaway")
  MEMORA_D1_TEST_EDIT_TOKEN     a D1 Edit token (setup and the replicator)
  MEMORA_D1_TEST_READ_TOKEN     a D1 Read token

What it establishes, for the plan's open "verify on a throwaway D1" steps:
- the D1 Read token can run a SELECT through /query and is refused an INSERT;
- the REST `batch` body is accepted and returns one result per statement;
- the replicator's writer and reader work end to end on the real API;
- the database reports its read-replication mode (must be disabled).
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from memora.backends import D1Connection, D1DefiniteError, D1SelectOnlyConnection
from memora.replicator import EPOCH_SQL, ReplicaD1Connection

ENV = ("MEMORA_D1_TEST_ACCOUNT", "MEMORA_D1_TEST_DATABASE", "MEMORA_D1_TEST_DATABASE_NAME",
       "MEMORA_D1_TEST_EDIT_TOKEN", "MEMORA_D1_TEST_READ_TOKEN")
_missing = [v for v in ENV if not os.getenv(v)]

pytestmark = [
    pytest.mark.skipif(bool(_missing), reason=f"live D1 checks need {', '.join(_missing)}"),
    pytest.mark.skipif("throwaway" not in os.getenv("MEMORA_D1_TEST_DATABASE_NAME", ""),
                       reason="MEMORA_D1_TEST_DATABASE_NAME must contain 'throwaway'"),
]

TABLE = "memories_meta"


@pytest.fixture(scope="module")
def ids():
    return os.environ["MEMORA_D1_TEST_ACCOUNT"], os.environ["MEMORA_D1_TEST_DATABASE"]


@pytest.fixture(scope="module")
def setup(ids):
    """The minimal schema the checks need, created with the edit token."""
    conn = D1Connection(*ids, os.environ["MEMORA_D1_TEST_EDIT_TOKEN"])
    conn._send("CREATE TABLE IF NOT EXISTS memories_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn._send("INSERT OR IGNORE INTO memories_meta (key, value) VALUES ('embedding_change_epoch', '0')")
    return conn


def test_read_token_selects_and_is_refused_an_insert(ids, setup):
    reader = D1SelectOnlyConnection(*ids, os.environ["MEMORA_D1_TEST_READ_TOKEN"])
    rows, meta = reader.execute(EPOCH_SQL)
    assert rows and "value" in rows[0]
    raw = D1Connection(*ids, os.environ["MEMORA_D1_TEST_READ_TOKEN"])
    with pytest.raises((D1DefiniteError, RuntimeError)):
        raw._send("INSERT INTO memories_meta (key, value) VALUES ('l3b-probe', 'x')")


def test_batch_body_is_accepted_and_returns_one_result_per_statement(ids, setup):
    writer = ReplicaD1Connection(*ids, os.environ["MEMORA_D1_TEST_EDIT_TOKEN"])
    upsert = ("INSERT INTO memories_meta (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    results = writer.execute_batch([(upsert, ("l3b-probe", "1")), (EPOCH_SQL, ())])
    assert len(results) == 2 and results[-1]["results"]
    writer.execute_batch([("DELETE FROM memories_meta WHERE key = ?", ("l3b-probe",))])


def test_read_replication_is_disabled(ids):
    account, database = ids
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}",
        headers={"Authorization": f"Bearer {os.environ['MEMORA_D1_TEST_READ_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        info = json.loads(resp.read())["result"]
    mode = (info.get("read_replication") or {}).get("mode", "disabled")
    assert mode == "disabled", f"read replication is {mode!r}; the plan requires it disabled"
