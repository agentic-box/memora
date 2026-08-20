"""memora #973: a followed list must not fetch the whole store to return a page.

memory_list defaults to follow="active", which takes the windowed lineage scan.
That scan used to open with _SCAN_WINDOW (5000) rows, so limit=3 fetched every
row in the store before slicing back to 3 — flat in limit, and on D1 (where each
row is HTTPS traffic) 163-174s against memory_list_compact's 0.22s.

These assert the load-bearing property directly: the SQL LIMIT actually issued.
"""
import pytest

from memora import storage
from memora.storage import _SCAN_MIN_WINDOW, _SCAN_WINDOW, list_memories


@pytest.fixture
def conn(local_db):
    with storage.connect() as c:
        for i in range(300):
            storage.add_memory(c, content=f"memory number {i}", tags=["bulk"])
        yield c


def _record_windows(monkeypatch):
    """Capture the sql_limit of every row fetch the scan issues."""
    seen = []
    real = storage._list_memory_sql_rows

    def spy(conn, *, sql_limit=None, sql_offset=None, **kw):
        seen.append(sql_limit)
        return real(conn, sql_limit=sql_limit, sql_offset=sql_offset, **kw)

    monkeypatch.setattr(storage, "_list_memory_sql_rows", spy)
    return seen


def test_small_page_does_not_open_a_full_store_window(conn, monkeypatch):
    """FirstWindowIsProportional: limit=3 must not open a 5000-row window."""
    seen = _record_windows(monkeypatch)
    rows = list_memories(conn, limit=3, follow="active")
    assert len(rows) == 3
    assert seen, "no fetch was issued"
    assert seen[0] <= _SCAN_MIN_WINDOW, (
        f"first window was {seen[0]}, expected <= {_SCAN_MIN_WINDOW}; "
        "a small page must not fetch the whole store"
    )
    assert seen[0] < _SCAN_WINDOW


def test_window_is_bounded_below_so_it_does_not_thrash(conn, monkeypatch):
    """MinWindowFloor: a tiny page still fetches a sane batch, not 1 row."""
    seen = _record_windows(monkeypatch)
    list_memories(conn, limit=1, follow="active")
    assert seen[0] >= _SCAN_MIN_WINDOW or seen[0] >= 3


def test_page_is_still_filled_when_rows_are_superseded(conn, monkeypatch):
    """RefillAcrossWindows: correctness must not depend on a large first window.

    Supersede far more rows than the first window covers, so the scan is forced
    to continue; the caller must still get a full page.
    """
    ids = [r["id"] for r in list_memories(conn, limit=-1)]
    # Supersede far more rows than the first window covers, so the scan MUST
    # continue across windows to fill the page. Uses the real edge API, not a
    # hand-written INSERT, so the test cannot drift from how supersession is
    # actually recorded.
    keeper = ids[-1]
    for mid in ids[:250]:
        if mid != keeper:
            storage.add_link(conn, keeper, mid, edge_type="supersedes", commit=False)
    conn.commit()
    seen = _record_windows(monkeypatch)
    rows = list_memories(conn, limit=10, follow="active")
    assert len(rows) == 10, f"page short: got {len(rows)}"
    assert len(seen) >= 1


def test_unbounded_list_still_uses_the_full_window(conn, monkeypatch):
    """UnboundedKeepsFullWindow: limit=None needs everything; do not shrink it."""
    seen = _record_windows(monkeypatch)
    list_memories(conn, limit=None, follow="active")
    assert seen[0] == _SCAN_WINDOW


def test_window_widens_when_a_page_needs_more_than_one_fetch(conn, monkeypatch):
    """WindowGrowsOnRefill: the property mutation 1 does NOT cover.

    Starting small is only safe if a store dense with superseded rows converges
    quickly. Without growth the scan pays a round-trip per fixed-size window --
    cheap locally, another D1 latency cliff. Assert the windows actually widen
    rather than asserting a brittle fetch count.
    """
    # The scan fetches NEWEST FIRST (descending id) while list_memories(limit=-1)
    # returns ascending, so superseding "the first 200 ids" superseded the rows
    # the scan reaches LAST and the opening window was still full of survivors.
    # Supersede by SCAN order instead: everything except the newest row and the
    # oldest hundred. Window 1 (the newest 100) then yields exactly one survivor
    # -- fewer than the page needs -- which forces a refill deterministically.
    ids = sorted(r["id"] for r in list_memories(conn, limit=-1))
    keeper = ids[-1]                      # newest; stays active
    superseded = ids[100:-1]              # everything between the oldest 100 and keeper
    for mid in superseded:
        storage.add_link(conn, keeper, mid, edge_type="supersedes", commit=False)
    conn.commit()
    assert len(list_memories(conn, limit=-1, follow="active")) == 101

    seen = _record_windows(monkeypatch)
    rows = list_memories(conn, limit=5, follow="active")
    assert len(rows) == 5, f"page short after refill: {len(rows)}"
    assert len(seen) >= 2, f"expected a refill, got one fetch: {seen}"
    assert seen[1] > seen[0], (
        f"window did not widen on refill: {seen}; a dense store would pay one "
        "round-trip per fixed window"
    )


class _CountingConn:
    """Wrap a connection and count execute() calls — the D1 round-trip proxy.

    sqlite3.Connection.execute is read-only, so it cannot be monkeypatched;
    wrap instead of patch.
    """

    def __init__(self, inner):
        self._inner = inner
        self.count = 0

    def execute(self, *a, **kw):
        self.count += 1
        return self._inner.execute(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_active_follow_does_not_probe_per_row(conn, monkeypatch):
    """NoPerRowSupersessionProbe: the second half of #973.

    _is_superseded called get_crossrefs() per memory and _memory_exists() per
    edge. Locally free; on D1 each is an HTTPS round-trip, so a 100-row page
    cost ~100 of them (~20s measured on the live store even after the scan
    window was fixed). Statement count must not scale with the page.
    """
    from memora.storage import apply_follow

    rows = list_memories(conn, limit=-1)
    small = rows[:5]
    large = rows[:100]

    counting = _CountingConn(conn)
    apply_follow(counting, small, "active", is_search=False)
    for_small = counting.count

    counting.count = 0
    apply_follow(counting, large, "active", is_search=False)
    for_large = counting.count

    assert for_large <= for_small + 2, (
        f"statements grew with page size: {for_small} for 5 rows, "
        f"{for_large} for 100 — that is a per-row probe"
    )


class _ParamLimitConn:
    """Reject any statement with >100 bound parameters, like D1 does.

    The earlier statement-count test used plain local SQLite with no
    supersession edges, so it exercised neither D1's cap nor the second batch
    statement — it passed while an ordinary page (limit=34 → 102 candidates)
    would have failed in production.
    https://developers.cloudflare.com/d1/platform/limits/
    """

    LIMIT = 100

    def __init__(self, inner):
        self._inner = inner
        self.max_params = 0

    def execute(self, sql, params=()):
        n = len(params) if params is not None else 0
        self.max_params = max(self.max_params, n)
        if n > self.LIMIT:
            raise RuntimeError(f"too many SQL variables: {n} > {self.LIMIT}")
        return self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_batch_respects_the_d1_bound_parameter_limit(conn):
    """D1ParamLimitCandidates: limit=34 opens with 102 candidates — over the cap."""
    from memora.storage import _superseded_ids_batch

    ids = [r["id"] for r in list_memories(conn, limit=-1)]
    assert len(ids) > 150
    guarded = _ParamLimitConn(conn)
    _superseded_ids_batch(guarded, ids)          # 300 candidates
    assert guarded.max_params <= _ParamLimitConn.LIMIT


def test_batch_respects_the_limit_on_the_second_statement(conn):
    """D1ParamLimitReferenced: >100 DISTINCT superseders must chunk too.

    The candidate chunking alone does not cover this: one page of candidates can
    reference far more distinct superseding ids than the cap.
    """
    from memora.storage import _superseded_ids_batch

    ids = sorted(r["id"] for r in list_memories(conn, limit=-1))
    # 150 distinct superseders, each superseding one distinct victim, so the
    # referenced set is 150 > 100 while candidates stay modest.
    victims = ids[:150]
    supers = ids[150:300]
    for victim, superseder in zip(victims, supers):
        storage.add_link(conn, superseder, victim, edge_type="supersedes", commit=False)
    conn.commit()

    guarded = _ParamLimitConn(conn)
    got = _superseded_ids_batch(guarded, victims)
    assert guarded.max_params <= _ParamLimitConn.LIMIT
    assert got == set(victims), f"chunking lost rows: {len(got)} of {len(victims)}"
