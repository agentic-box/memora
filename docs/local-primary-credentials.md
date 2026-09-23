# Local-primary credentials: inventory, minting, repointing, rotation

Implements `docs/local-primary-implementation.md` §6 F4a–F6 and §6.1 (slice
L8). Every step here is run by the user; no agent holds a Cloudflare
credential that can write D1 or deploy Pages.

## 1. Who holds what

Cloudflare D1 token permissions are account-scoped ("D1 Read" or "D1 Edit");
there is no per-database scope. So tokens are separated by **holder and
purpose**, not by database.

| credential | Cloudflare permission | held by | delivered as |
|---|---|---|---|
| (a) `MEMORA_D1_EDIT_TOKEN` | Account → D1 → **Edit** | memora-all, for every store still served from `d1://` (all stores until their cutover, including each shadow week) | `CLOUDFLARE_API_TOKEN` in memora-all's env (the name `D1Backend` reads), from `~/.config/memora/credentials.mcp.json` on nuc8. No code change |
| (b) `MEMORA_D1_REPLICATOR_TOKEN` | Account → D1 → **Edit** | memora-all's replicator, write mode only | `MEMORA_D1_REPLICATOR_TOKEN`, read only by `memora/replicator.py` |
| (c) `MEMORA_D1_READ_TOKEN` | Account → D1 → **Read** | the shadow applier's reader; `local_primary.py` export, recheck and compare | `MEMORA_D1_READ_TOKEN`, or `--read-token-file`; given to wrangler only as the subprocess's `CLOUDFLARE_API_TOKEN` |
| operator | D1 Edit: token (b), used by a person | whoever runs `local_primary.py` restore apply, the sequence step or `restamp` | a 0600 file passed with `--credential-file`; never a service env. Each use also needs a receipt |
| Pages deploy | Pages (no D1) | the user only; on no agent host | a `wrangler login` session or a Pages-only token on the user's machine |
| memora-all health token | — | operators, probes | `~/.config/memora/all.health-token` (0600) on nuc8 |
| memora-all admin token | — | operators, `local_primary.py` | `~/.config/memora/all.admin-token` (0600) on nuc8; never equal to the health token |

Tokens (a), (b) and (c) carry **no Pages permission**.

By phase:

| phase | memora-all | scripts on nuc8 | other hosts |
|---|---|---|---|
| before F6 (L1b–L8) | the OLD token | none | the OLD token (Mac MCP etc.) |
| after F6, before any shadow | (a) | (c) for exports | nothing: repointed to nuc8 (F4, F5) |
| store X in its shadow week | (a) plus (c) (shadow reader) | (c) | nothing |
| store X cut over, others not | (a) for the uncut stores; (b) for X's replicator; (c) while any store is in shadow | (c); operator (b) for restore, sequence or restamp | nothing |
| after L12, rollback window open (14 days after L12's clean write week) | (a) still held, so a rollback remains possible; (b); (c) for nightly compares | (c); operator | nothing |
| window closed | (b); (c) | (c); operator | nothing. (a) is revoked; a later rollback needs a new edit token |

## 2. Minting (Cloudflare dashboard → My Profile → API Tokens → Create Token → Custom token)

For each of (a), (b), (c):

1. **Permissions**: one row, `Account` · `D1` · `Edit` for (a) and (b), or
   `Account` · `D1` · `Read` for (c). Nothing else: no Pages, no Workers, no
   R2 (R2 uses its own S3 keys).
2. **Account resources**: `Include` · the one account that holds the memora D1
   databases.
3. **Client IP filtering** (recommended): nuc8's egress address for (a) and
   (b); nuc8 plus any operator host for (c).
4. **TTL**: none for (a)–(c); they are revoked by the rotation below.
5. Name them `memora-d1-edit`, `memora-d1-replicator` and `memora-d1-read`,
   so the dashboard shows what each one is.
6. Verify each one: `curl -H "Authorization: Bearer <token>"
   https://api.cloudflare.com/client/v4/user/tokens/verify` must answer
   `"status": "active"`.
7. Store each in a 0600 file on nuc8 only (for example
   `~/.config/memora/d1-edit.token`); never in a repo, an instance `.env` or a
   workspace `.mcp.json`.

## 3. Rotation order: mint → repoint → verify → revoke

The OLD token (the one in the Mac MCP config and on other hosts) is revoked
last, only after nothing but memora-all uses D1.

1. **Mint** (a), (b) and (c) (section 2).
2. **Scratch store (F4a)**: add a local store to memora-all's registry, for
   example `"scratch": "/data/scratch.db"`, redeploy, and prove the endpoint
   from the Mac:

   ```sh
   scripts/local_primary.py check-endpoint --memora-url http://nuc8:8920 \
     --health-token-file ~/.config/memora/nuc8.health-token \
     --admin-token-file ~/.config/memora/nuc8.admin-token --store scratch
   ```

   It proves liveness, that the admin token is enforced (no token and the
   health token are refused), that `scratch` is a local SQLite store, the
   authenticated per-store health, and an MCP create → get → delete →
   gone round trip in `scratch`. It refuses to write any store that is not a
   local SQLite store. D1 is never touched.
3. **Repoint every client (F4, F5)**, one file at a time, dry run first:

   ```sh
   scripts/repoint_mcp_config.py ~/.claude.json --url http://nuc8:8920/mcp/memora
   scripts/repoint_mcp_config.py ~/.claude.json --url http://nuc8:8920/mcp/memora --apply \
     --check-health-token-file ~/.config/memora/nuc8.health-token \
     --check-admin-token-file ~/.config/memora/nuc8.admin-token
   ```

   Each direct-D1 stdio entry becomes `{"type": "http", "url": …}`; a 0600
   backup `FILE.bak-repoint-<timestamp>` is written first. With the check
   flags, step 2's endpoint check runs first against the URL's server,
   through the scratch store (`--check-store`, default `scratch`), and a
   failure refuses the repoint. Repeat for every file the audit (step 4) lists: workspace
   `.mcp.json` files, `~/.claude.json`, `~/.config/memora/*credentials*.mcp.json`
   on the Mac, ob1, bestation and re. Repointing
   `~/.config/memora/credentials.mcp.json` on the Mac also retires the Mac's
   own `memora-instance.sh` containers as D1 clients: that file is their
   credential source.
4. **Audit (F4/F5 done when clean)**:

   ```sh
   scripts/audit_configs.py --local --host nuc8 --host ob1 --host bestation --host re
   ```

   Exit 0 only when no host has a direct-D1 client except memora-all itself
   (`instances/all.env`; on nuc8, `~/.config/memora/credentials.mcp.json`
   and `all.*`). A host that cannot be audited counts as not clean.
5. **Move memora-all to (a) (F6)**: put (a) as `CLOUDFLARE_API_TOKEN` in
   nuc8's `~/.config/memora/credentials.mcp.json` (a backup is kept by the
   deploy), redeploy, and check `/health/db/<store>` for every store.
6. **Revoke the OLD token** in the dashboard. Then delete every
   `*.bak-repoint-*` backup: each still holds it. A stale client now gets
   401/403 from Cloudflare; memora-all stays healthy on (a).
7. (b) goes into memora-all's env when the first store's replicator is
   switched to write mode (L9); (c) when the first shadow starts or the first
   export runs.

## 4. Retention of (a) after the last cutover

After L12 (`memora`, the last store) has run its clean write week, (a) is
**kept for 14 more days**: while it exists, `local_primary.py` rollback can
repoint a store back to `d1://` (plan §5.3). When the 14 days end without a
rollback, revoke (a); memora-all then holds only (b) and (c), and a later
rollback needs a newly minted edit token.
