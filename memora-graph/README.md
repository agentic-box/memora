# Memora Graph - Cloudflare Deployment

Cloud-hosted knowledge graph visualization for Memora, deployed on Cloudflare Pages with D1 database and real-time WebSocket updates.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   MCP Server    │────▶│   R2 Storage    │────▶│   D1 Database   │
│   (Local)       │     │   (Primary)     │     │   (Read-only)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                                               │
        │ WebSocket broadcast                           │
        ▼                                               ▼
┌─────────────────┐                           ┌─────────────────┐
│   DO Worker     │◀──────────────────────────│  Pages (Graph)  │
│   (WebSocket)   │                           │   UI + API      │
└─────────────────┘                           └─────────────────┘
```

- **R2**: Primary storage (authoritative source)
- **D1**: Read-only copy for web UI queries
- **Pages**: Static graph UI + API functions
- **DO Worker**: Durable Object for WebSocket connections (real-time updates)

## Read-only viewer

The deployed viewer never writes D1 (`docs/local-primary-implementation.md`
§6 F1, slice L7). memora-all on nuc8 is the only D1 writer; memories are
created and edited through memora itself.

- `PATCH`, `PUT`, `POST` and `DELETE` on `/api/memories/:id` answer `405`
  (`{"error": "read_only"}`, `Allow: GET, HEAD`) without touching D1.
- `/api/chat` searches and answers. The model is offered no tools, and a tool
  call it emits anyway is not executed; the answer says no memory was changed.
- `GET /api/capabilities` answers `{"read_only": true}`. The shared page
  (`public/index.html`, the same file memora's own graph server serves) shows
  a "Read-only viewer" badge and hides or disables its edit controls unless a
  server answers `read_only: false`, which only memora's local graph server
  does. `force-graph.html` has no edit controls.
- `scripts/d1_write_guard.py --scope all` is clean and blocking in CI
  (`graph-ui.yml`), and `npm run deploy` runs it first.
- Tests: `node --experimental-strip-types scripts/test_readonly.mjs [baseUrl]`.

## Quick Setup (partial: stops after creating the D1 database)

```bash
cd memora-graph
npm run setup
```

`npm run setup` is no longer a full automated setup. It checks the
prerequisites, installs dependencies, checks the Cloudflare login and creates
the D1 database if it is missing, then **exits 1** at the retired remote
migration step (remote D1 writes are retired: `docs/local-primary-implementation.md`
§0 P6, §6 F3). It never reaches its later steps (Worker, Pages, bindings,
initial sync). Follow the manual steps below for the rest; the D1 database is
filled by memora-all, not by this repository.

## Manual Setup

### Prerequisites

- Node.js 18+
- Cloudflare account
- R2 bucket named `memora` (for existing Memora data)

### 1. Install dependencies

```bash
npm install
cd worker && npm install && cd ..
```

### 2. Login to Cloudflare

```bash
npx wrangler login
```

### 3. Create D1 database

```bash
npx wrangler d1 create memora-graph
```

Update `wrangler.toml` with the database ID from the output.

### 4. Run migrations

Retired: memora-all on nuc8 is the only D1 writer (see `docs/local-primary-implementation.md` §0 P6, §6 F3). Remote D1 migrations are disabled; `npm run d1:migrate` exits 1. `npm run d1:migrate-local` still works for local development.

### 5. Deploy WebSocket Worker

```bash
cd worker
npx wrangler deploy
cd ..
```

Note the worker URL (e.g., `https://memora-graph-sync.xxx.workers.dev`)

### 6. Update worker URL

Edit `public/index.html` and update the WebSocket URL:
```javascript
var wsUrl = 'wss://memora-graph-sync.YOUR-SUBDOMAIN.workers.dev/ws';
```

### 7. Create Pages project

```bash
npx wrangler pages project create memora-graph --production-branch=main
```

### 8. Configure bindings

In Cloudflare Dashboard:
1. Go to Workers & Pages > memora-graph > Settings > Bindings
2. Add D1 binding: `DB` → `memora-graph`
3. Add R2 binding: `R2` → `memora`

### 9. Deploy Pages

```bash
npm run deploy
```

`npm run deploy` runs `scripts/d1_write_guard.py --scope all` first and refuses on any finding. Since slice L7 the viewer is read-only and the guard is clean, so the deploy proceeds. Deploy through `npm run deploy` only: a direct `wrangler pages deploy` skips the guard (plan §0 P6). The Pages deploy credential is held by the user only.

### 10. Initial sync

Retired: memora-all on nuc8 is the only D1 writer (see `docs/local-primary-implementation.md` §0 P6, §6 F3). `npm run sync-remote` (`sync.sh --remote`) exits 1; `npm run sync` still syncs to a local D1.

## Enable Auto-Sync

Add to your `.mcp.json` environment:

```json
{
  "env": {
    "MEMORA_CLOUD_GRAPH_ENABLED": "true"
  }
}
```

Now any memory create/update/delete will automatically sync to the cloud graph and push updates to connected browsers.

## Scripts

| Script | Description |
|--------|-------------|
| `npm run setup` | Partial: prerequisites, dependencies, login and D1 database creation, then exits 1 at the retired remote migration step |
| `npm run deploy` | Deploy Pages site (runs the D1 write guard first; refuses on findings) |
| `npm run deploy:worker` | Deploy WebSocket worker |
| `npm run sync` | Sync to a local D1 (development) |
| `npm run sync-remote` | Retired: exits 1 |
| `npm run d1:migrate` | Retired: exits 1 (use `d1:migrate-local`) |
| `npm run dev` | Local development server |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MEMORA_CLOUD_GRAPH_ENABLED` | Enable auto-sync | `false` |
| `MEMORA_CLOUD_GRAPH_WORKER_URL` | WebSocket worker URL | _(required when cloud graph sync is enabled)_ |
| `MEMORA_CLOUD_GRAPH_SYNC_SCRIPT` | Path to sync script | Auto-detected |
| `MIN_EDGE_SCORE` | Minimum similarity for graph edges | `0.40` |

## Project Structure

```
memora-graph/
├── functions/
│   └── api/
│       ├── graph.ts           # GET /api/graph - returns nodes/edges
│       ├── memories.ts        # GET /api/memories - returns all memories
│       ├── memories/
│       │   └── [id].ts        # GET /api/memories/:id - single memory
│       └── r2/
│           └── [[path]].ts    # Proxy images from R2
├── public/
│   └── index.html             # Graph SPA
├── scripts/
│   ├── setup-cloudflare.sh    # Partial setup: stops at the retired remote migration
│   ├── sync.sh                # Sync wrapper with env loading (local D1 only)
│   ├── sync-to-d1.py          # Export to a local D1 (remote runs exit 1)
│   └── link-r2-images.py      # Retired: exits 1
├── worker/
│   └── src/
│       └── index.ts           # Durable Object for WebSocket
├── migrations/
│   └── 0001_init.sql          # D1 schema
├── wrangler.toml
├── package.json
└── tsconfig.json
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/graph` | Returns graph nodes, edges, and metadata |
| `GET /api/memories` | Returns all memories for timeline |
| `GET /api/memories/:id` | Returns single memory by ID |
| `GET /api/r2/*` | Proxies images from R2 storage |

## Security Model

Memora is a **single-user** memory system. All memories in a database are
accessible to any authenticated user. Multi-user/multi-tenant isolation is
not supported and the `?db=` parameter is not a tenant boundary — it selects
between the owner's own databases.

Access control is enforced at the infrastructure level:
- **Cloud:** Cloudflare Access gates all Pages endpoints (authentication required)
- **Local:** Graph server binds to localhost by default
- **MCP:** Server runs as a local process under the user's own permissions

### Rate Limiting

- **Cloud chat:** Cloudflare Rate Limiting rule — 30 req/min per IP for `/api/chat`
- **Local chat:** Built-in middleware — 30 req/min per IP for `/api/chat`
- **MCP tools:** Operation-specific cooldowns on expensive tools (rebuild, export, import)

### Local Cache

When using cloud backends (S3/R2), a local SQLite cache is stored at
`~/.cache/memora/`. This cache is unencrypted. For sensitive data,
ensure your disk uses full-disk encryption.

## Troubleshooting

### "wrangler: command not found"
Run `npm install` first, then use `npx wrangler` or `npm run` scripts.

### D1 bindings not working
Ensure bindings are configured in Cloudflare Dashboard under Pages project settings.

### WebSocket not connecting
Check that the DO Worker is deployed and the URL in `index.html` matches.

### Sync not updating UI
1. Check `MEMORA_CLOUD_GRAPH_ENABLED=true` in `.mcp.json`
2. Restart MCP server after config changes
3. Verify WebSocket is connected (browser console)
