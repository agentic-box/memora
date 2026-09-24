#!/bin/bash
# Sync memora data to D1, loading environment from .mcp.json
#
# Usage:
#   ./scripts/sync.sh           # Local D1 (development) only
#
# RETIRED for remote D1: memora-all on deploy-host is the only D1 writer
# (docs/local-primary-implementation.md §0 P6, §6 F3). Any remote run exits 1
# before it reads .mcp.json, runs python or calls the broadcast endpoint.

for arg in "$@"; do
    case "$arg" in
        --remote|--remote=*)  # abbreviations are rejected by sync-to-d1.py (allow_abbrev=False)
            echo "sync.sh: remote D1 sync is retired. memora-all on deploy-host is the only D1 writer; see docs/local-primary-implementation.md §0 P6 and §6 F3." >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MEMORA_ROOT="$(dirname "$PROJECT_ROOT")"
MCP_CONFIG="$MEMORA_ROOT/.mcp.json"

if [ ! -f "$MCP_CONFIG" ]; then
    echo "Error: .mcp.json not found at $MCP_CONFIG"
    exit 1
fi

# Extract environment variables from .mcp.json using python
ENV_VARS=$(python3 -c "
import json
import sys

with open('$MCP_CONFIG') as f:
    config = json.load(f)

env = config.get('mcpServers', {}).get('memora', {}).get('env', {})
for key, value in env.items():
    # Only export storage-related vars
    if key.startswith(('AWS_', 'MEMORA_STORAGE', 'MEMORA_CLOUD')):
        print(f'export {key}=\"{value}\"')
")

if [ -z "$ENV_VARS" ]; then
    echo "Error: Could not extract environment from .mcp.json"
    exit 1
fi

# Export the variables
eval "$ENV_VARS"

echo "Loaded environment from .mcp.json:"
echo "  MEMORA_STORAGE_URI=$MEMORA_STORAGE_URI"
echo "  AWS_PROFILE=$AWS_PROFILE"
echo ""

# Run the sync script
cd "$PROJECT_ROOT"
python scripts/sync-to-d1.py "$@"
