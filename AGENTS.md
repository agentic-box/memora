# Working in memora

memora is a SQLite-backed memory server (MCP + a plain HTTP API). Read
`README.md` for behaviour, `docs/` for subsystem design, and
`contracts/memora-api/v1/README.md` for the API contract.

## Running tests

- Install the dev extras once: `pip install -e '.[dev]'`.
- Run **targeted** tests, not the whole suite: `python -m pytest -q <files>`.
  The full suite is the leader's to run, once per candidate commit.
- Run a passing candidate on a Linux build host (`<build-host>`; venvs
  `~/verify/venv312` and the 3.10 floor `~/verify/venv310`) **and** on the Mac
  test host (`<mac-test-host>`). CI's floor is Python 3.10 (see
  `.github/workflows/clean-install.yml`); a change must pass there.
- `tests/test_no_infra_identifiers.py` scans `git ls-files`, so it needs a real
  git checkout — run it from a worktree, never from an extracted tarball.
- Graph UI changes (`memora/graph/**`, `memora-graph/**`) are covered by
  `memora-graph/scripts/test_ui.mjs` (the `graph-ui` CI job), not pytest.
- Before a review request, run only the tests you added or changed. After a
  PASS, run the targeted set plus a mutation (make each claimed test fail
  once). See the sealed-review skill.

### Touched module → test file

Tests are named after their subject. Start from the map below, then add every
test file that imports the touched module.

| Touched | Test file(s) |
|---|---|
| `memora/<x>.py` | `tests/test_<x>.py` when it exists |
| `memora/api_v1.py`, `api_absorb.py` | `tests/test_api_v1.py`, `tests/test_api_absorb.py`, `tests/test_api_contract.py` |
| `memora/graph/**` | `tests/test_graph_server.py`, `memora-graph/scripts/test_ui.mjs` |
| `contracts/memora-api/v1/**` | `tests/test_api_contract.py` |
| `scripts/deploy*`, `Dockerfile` | `tests/test_deploy_memora_all.py`, `tests/test_deploy_config.py` |
| `scripts/<x>.py`, `scripts/<x>.sh` | `tests/test_<x>.py` where one exists (e.g. `mint_api_token.sh` → `tests/test_mint_api_token.py`) |
| `scripts/memora_api_contract.py` | `tests/test_api_contract.py` |

## Frozen contract

`contracts/memora-api/v1/**` is **byte-frozen** at its tag
`memora-api-v1.0.1`. Do not edit any file in it — a schema, fixture, VERSION or
README byte change is a new contract version and tag, never an in-place edit.
Documentation and design notes belong in `docs/`, not in the contract
directory. The clmux repo vendors this directory and pins it with a checksum
test; an edit here that is not re-vendored there breaks that test.

## Commit hygiene

- One git worktree per item, on a branch off the named base commit. Never
  edit, build or commit in the shared checkout.
- One commit per review round. Never amend or rewrite a commit already sent
  for review; add a new commit on top.
- When pausing mid-item, put a `STATE:` note in the commit body: what is done,
  what is left, and any blocker.

## Evidence

Report the exact command, host, real exit code and pass counts. A red leg is
red: never call a failure flaky, never call a partial run green.

## Pointers

- Sealed review: `skills/sealed-review/SKILL.md` in the clmux repo (pi also
  installs it at `~/.pi/agent/skills/sealed-review/SKILL.md`).
- Plan/design docs: `docs/`, `contracts/memora-api/v1/README.md`.
