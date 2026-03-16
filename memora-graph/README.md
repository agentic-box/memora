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

## Quick Setup

```bash
cd memora-graph
npm run setup
```

This will:
1. Install dependencies
2. Create D1 database
3. Deploy WebSocket Worker (Durable Object)
4. Deploy Pages site
5. Guide you through binding configuration
6. Run initial data sync

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

```bash
npx wrangler d1 execute memora-graph --remote --file=migrations/0001_init.sql
```

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

### 10. Initial sync

```bash
npm run sync-remote
```

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
| `npm run setup` | Full automated setup |
| `npm run deploy` | Deploy Pages site |
| `npm run deploy:worker` | Deploy WebSocket worker |
| `npm run sync-remote` | Manual sync R2 → D1 |
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
│   ├── setup-cloudflare.sh    # Automated setup script
│   ├── sync.sh                # Sync wrapper with env loading
│   └── sync-to-d1.py          # Export R2 data → D1
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
