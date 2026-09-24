# Deploy rehearsal (R1)

`scripts/rehearse_deploy.sh` runs the real `scripts/deploy-memora-all.sh` —
the L2a named-volume migration and the new container — against a real
container runtime on a rehearsal host (server2, rootless podman 5.8). It uses
no Cloudflare, no nuc8, and no token that exists anywhere else.

```
git archive v0.4.6 | ssh server2 'mkdir -p ~/verify/r1-old-src && tar -x -C ~/verify/r1-old-src'
rsync -a --exclude .git ./ server2:verify/feat-r1-rehearsal/
ssh server2 'cd ~/verify/feat-r1-rehearsal && PYTHON=~/verify/venv312/bin/python scripts/rehearse_deploy.sh ~/verify/r1-old-src'
```

Everything it creates is suffixed `-rh`: the container `memora-rh`, the
volume `memora-rh-data`, the image `memora-rh:latest`, port 18920. State lives
under `~/rehearsal-r1` (`results.txt`, `commands.log`, `deploy-*.log`,
`fixtures/`). A rerun removes only what an earlier run recorded. It never
prunes, because pruning would delete the host user's other unused volumes.

## The deploy script's rehearsal parameters

Every default is the production value, so an unparameterised run is exactly
the nuc8 deploy; `tests/test_deploy_memora_all.py` pins both.

| variable | default | rehearsal |
|---|---|---|
| `DEPLOY_HOST` | `nuc8` | `localhost` (no ssh; the same remote script runs locally) |
| `RUNTIME` | `docker` | `podman` (every runtime call goes through it) |
| `DEPLOY_CONTAINER` / `DEPLOY_DATA_VOLUME` / `DEPLOY_IMAGE` | `memora-all` / `memora-all-data` / `memora:latest` | `memora-rh` / `memora-rh-data` / `memora-rh:latest` |
| `DEPLOY_PORT` | `8920` | `18920` |
| `DEPLOY_CONFIG_DIR` | `~/.config/memora` | `~/rehearsal-r1/config` (throwaway 0600 tokens) |
| `DEPLOY_ENV_FILE` | `instances/all.env` | `~/rehearsal-r1/all.env` (four local SQLite stores) |
| `DEPLOY_REPO` / `DEPLOY_SKIP_CHECKOUT` | the nuc8 checkout / `0` | this checkout / `1` (build it as it is) |
| `DEPLOY_TAG` | `v0.4.6` | `v<pyproject version>` |
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
6. `local_primary.py freeze` and `thaw` go through the live admin routes.
   `/health/db` shows `frozen` with 0 in flight, and a write is refused.
   `compare --mode barrier` reaches the live store and refuses because
   replication is not installed. The D1 steps are skipped because there is
   no D1 here.
7. `podman inspect` output of the old and the new container is saved, with
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
  A release deploy with a new tag distinguishes them.
- **`compare` on a store without replication raised a raw SQLite error**
  (`no such table: sync_state`). It now refuses cleanly: "replication is not
  installed on this store".
- **`memora-instance.sh` is not podman-compatible**, and this is not
  changed here. It lists containers with `list --all`, which is Apple
  `container` syntax (`podman container list --all` would be needed). Only
  its inspect parser is checked against podman's real output.
