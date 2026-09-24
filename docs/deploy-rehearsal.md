# Deploy rehearsal (R1)

`scripts/rehearse_deploy.sh` runs the real `scripts/deploy-memora-all.sh` —
the L2a named-volume migration and the new container — against a real
container runtime on a rehearsal host (build-host, rootless podman 5.8). It uses
no Cloudflare, no deploy-host, and no token that exists anywhere else.

```
git archive v0.4.6 | ssh build-host 'mkdir -p ~/verify/r1-old-src && tar -x -C ~/verify/r1-old-src'
rsync -a --exclude .git ./ build-host:verify/feat-r1-rehearsal/
ssh build-host 'cd ~/verify/feat-r1-rehearsal && PYTHON=~/verify/venv312/bin/python scripts/rehearse_deploy.sh ~/verify/r1-old-src'
```

Everything it creates is suffixed `-rh`: the container `memora-rh`, the
volume `memora-rh-data`, the image `memora-rh:latest`, port 18920. State lives
under `~/rehearsal-r1` (`results.txt`, `commands.log`, `deploy-*.log`,
`fixtures/`). **Objects (review 7747): structurally scoped to one run.** Every runtime
object is created and removed through `scripts/rehearse_objects.sh`:
- **Per-run names.** Each run has a `RUN_ID` (`rh-<epoch>-<pid>`), and every
  name contains it: container `memora-<RUN_ID>`, volume
  `memora-<RUN_ID>-data`, image `memora-<RUN_ID>:latest`, scratch volumes,
  probe. There are no fixed names.
- **Captured creates.** Every create labels the object
  `memora.rehearsal=<RUN_ID>` and captures its ID only from a successful
  create, or dies: `new_container`, `new_volume`, `new_image`, helpers via
  `once` (create, start attached, remove), and the `run -d --rm` hazard
  demo.
- **Adopted deploy objects.** What the deploy creates is labelled through
  `DEPLOY_LABELS` (container, data volume, migrator, image build). It is
  adopted by its per-run name only when it carries this run's label.
- **Teardown** runs on exit (`RH_KEEP=1` keeps the objects; then
  `scripts/rehearse_teardown.sh <run-objects file>`).
  - It removes only the recorded objects, by ID, after re-checking the
    label.
  - An image is never removed by ID (review 7751). Only the tags the run
    created and recorded are untagged, each while it still resolves to the
    captured, labelled image:
    - its per-run build tag, recorded at creation;
    - the deploy's `memora-<RUN_ID>:latest` and `rollback-<ts>` tags,
      recorded at adoption.
    Any other tag stays, and so does the image while such a tag remains.
  - An anonymous `/data` volume goes only when it is flagged anonymous,
    unused, and created during the run.
  - Nothing is ever removed by name or pruned.
- **Self-test.** `scripts/rehearse_cleanup_selftest.sh IMAGE` checks 23
  rules against the real runtime; its decoys carry per-run names and a
  `decoy-of` label.
- **Survival check.** `scripts/rehearse_survival_check.sh OLD_SRC IMAGE`
  creates `rh-helper-backup` and `memora-rh-scratch8` (the old fixed names)
  and checks that they survive a full rehearsal and the self-test.

## The deploy script's rehearsal parameters

Every default is the production value, so an unparameterised run is exactly
the deploy-host deploy; `tests/test_deploy_memora_all.py` pins both.

**The guard (review 7725).**
- Any value that differs from production is refused unless
  `DEPLOY_REHEARSAL=1` is set. Only `rehearse_deploy.sh` sets it.
- With the sentinel, a preflight refuses before anything runs unless all of
  these hold:
  - `DEPLOY_REHEARSAL_ROOT` is an existing directory;
  - `DEPLOY_HOST=localhost` (deploy-host is refused);
  - the container, volume and image names contain `-rh`;
  - the port is not 8920;
  - the config dir and the env file are under the rehearsal root.
- Every run prints its effective target (host, runtime, container, volume,
  image, port, tag) before any git or runtime step.

| variable | default | rehearsal |
|---|---|---|
| `DEPLOY_HOST` | `deploy-host` | `localhost` (no ssh; the same remote command line runs through `sh -c`, re-parsed as ssh's remote shell would: REL2) |
| `RUNTIME` | `docker` | `podman` (every runtime call goes through it) |
| `DEPLOY_CONTAINER` / `DEPLOY_DATA_VOLUME` / `DEPLOY_IMAGE` | `memora-all` / `memora-all-data` / `memora:latest` | `memora-rh` / `memora-rh-data` / `memora-rh:latest` |
| `DEPLOY_PORT` | `8920` | `18920` |
| `DEPLOY_CONFIG_DIR` | `~/.config/memora` | `~/rehearsal-r1/config` (throwaway 0600 tokens) |
| `DEPLOY_ENV_FILE` | `instances/all.env` | `~/rehearsal-r1/all.env` (four local SQLite stores) |
| `DEPLOY_REPO` / `DEPLOY_SKIP_CHECKOUT` | the deploy-host checkout / `0` | this checkout / `1` (build it as it is) |
| `DEPLOY_TAG` | `v0.5.0` | `v<pyproject version>` |
| `DEPLOY_SECRETS_DIR` | `~/.config/memora-lp` | `~/rehearsal-r1/secrets` (throwaway 0600 token files, mounted read-only) |
| `DEPLOY_SMOKE_ABSORB` | `1` | `0` (the dry-run absorb needs the LLM) |

## What it checks

1. The old image is built from the v0.4.6 source, which is what production
   runs today.
2. The old container is started the production way. The image declares
   `VOLUME /data`, so `/data` is an anonymous 64-hex volume. It then leaves
   data behind for the copy:
   - memories in four stores;
   - an intent journal file;
   - WAL sidecars on a probe database, held by a process that dies with the
     container.
3. The deploy runs. The checks after it:
   - the old container is kept, renamed and stopped, and still mounts its
     volume;
   - the new container mounts the named volume;
   - the marker names the source, and its digest is the old volume's digest
     (independently recomputed);
   - a second copy by the same program on this runtime is byte-identical,
     file by file;
   - the WAL sidecar and the intent journal reached the named volume
     unchanged;
   - the memory limit is 960m;
   - `/health` answers;
   - the minted admin token file is 0600;
   - `/admin/data-volume` answers 200 to the admin token, and 401 to the
     health token and to no token;
   - every store is `sqlite`, needs the data volume and is not refused,
     which means the startup `/data` check passed.

   The new server writes to the named volume as soon as it starts (the
   schema upgrade). That is why the copy is compared through the marker and
   an independent copy, not against the live volume.
4. A second deploy runs no copy, and the marker is unchanged.
5. Rollback: the new container is removed, and the ORIGINAL old container is
   renamed back, started and written to. A redeploy then copies again: the
   marker records the old volume's new digest, and the replaced content is
   kept in `.memora-previous-*`.
   - (REL1) The container's env has only the `*_FILE` paths of the
     Cloudflare tokens, and no token value. `/run/secrets/memora` is
     mounted read-only (`RW: false`, and a write inside fails). The
     server's rule (`memora/secret_files.py`) reads every file through the
     mount under rootless podman's uid mapping.
6. `local_primary.py freeze` and `thaw` go through the live admin routes.
   `/health/db` shows `frozen` with 0 in flight, and a write is refused.
   `compare --mode barrier` reaches the live store and refuses because
   replication is not installed. The D1 steps are skipped because there is
   no D1 here.
7. (REL1) The cutover mechanics, run inside the container the way
   `scripts/cutover_store.sh` runs them:
   - the operator tool at `/app/scripts/local_primary.py`;
   - the admin and health tokens as 0600 files on the container's tmpfs,
     written by the container's own shell;
   - `freeze gamma` and `fk-audit gamma`.

   Sync is then installed on `/data/gamma.db`, standing in for the seed,
   which needs D1. A deploy follows, with `MEMORA_REPLICAS={"gamma": …}` and
   `MEMORA_REPLICATION=log` in `all.env`. Checked after it:
   - `gamma` comes up frozen, from the persisted freeze;
   - it replicates in **log** mode, not halted;
   - after `thaw`, a write through `/mcp/gamma` is logged: `lag_rows` 0, and
     the log cursor moved.

   Log mode sends nothing to D1. **Write mode is not rehearsed**: the
   replicator's D1 endpoint is fixed to Cloudflare's, and no fake D1 HTTP
   server exists. Write mode against real D1 first runs in the live
   cutover (`docs/cutover-runbook.md`).
8. `podman inspect` output of the old and the new container is saved, with
   token values redacted. The copies in `tests/fixtures/` check the
   launchers' inspect parsing against the real shape.

## Findings

- **podman's `run --rm` deletes a named anonymous volume.**
  `podman run --rm -v <64-hex anonymous volume>:/from:ro …` deletes that
  volume when the container exits, if no other container still references
  it. Docker keeps a volume mounted by name.
  - The migration used `run --rm`. During a deploy, the stopped old
    container still references the volume, which is what saved it in the
    rehearsal. Any path where it does not would lose the old data: a
    runtime without `rename` (the instance script then removes the old
    container), or a partial earlier run.
  - Both launchers now run the migration as a named container and remove it
    with a plain `rm`, which never removes volumes.
  - The rehearsal demonstrates the hazard on a probe volume and checks that
    the fixed form keeps it.
  - `deploy-memora-all.sh` also mounts `--tmpfs /data` on the migration
    container, so the image's `VOLUME /data` leaves no empty volume behind.
    `memora-instance.sh` does not, because `--tmpfs` is unverified on
    Apple's `container`.
- **The version check cannot tell old from new here.** v0.4.6 and this
  branch both report 0.4.6. The rehearsal proves the new code serves with
  the admin route and the startup check instead, since v0.4.6 has neither.
  From v0.5.0 (REL1) the versions differ, and the `/health` version check
  tells the two images apart.
  A release deploy with a new tag distinguishes them.
- **`compare` on a store without replication raised a raw SQLite error**
  (`no such table: sync_state`). It now refuses cleanly: "replication is not
  installed on this store".
- **`memora-instance.sh` is not podman-compatible**, and this is not
  changed here. It lists containers with `list --all`, which is Apple
  `container` syntax (`podman container list --all` would be needed). Only
  its inspect parser is checked against podman's real output.
