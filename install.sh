#!/usr/bin/env bash
# Memora install script — installs server, skill file, and optionally configures MCP
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$REPO_DIR/skills/memora/SKILL.md"
SKILL_DST="$HOME/.claude/skills/memora/SKILL.md"

echo "=== Memora Installer ==="
echo ""

# 1. Install memora-server via pipx
echo "[1/3] Installing memora-server..."
if command -v pipx &>/dev/null; then
    pipx install -e "$REPO_DIR" --force 2>&1 | tail -3
elif command -v pip &>/dev/null; then
    echo "  pipx not found, falling back to pip..."
    pip install -e "$REPO_DIR" 2>&1 | tail -3
elif command -v pip3 &>/dev/null; then
    echo "  pipx not found, falling back to pip3..."
    pip3 install -e "$REPO_DIR" 2>&1 | tail -3
else
    echo "  ERROR: No pip/pipx found. Install manually: pip install -e $REPO_DIR"
    exit 1
fi

# Verify
if command -v memora-server &>/dev/null; then
    VERSION=$(memora-server --version 2>/dev/null || echo "unknown")
    echo "  memora-server installed: $VERSION"
else
    echo "  WARNING: memora-server not found on PATH after install"
fi

# 2. Install skill file
echo ""
echo "[2/3] Installing Claude Code skill..."
if [ -f "$SKILL_SRC" ]; then
    mkdir -p "$(dirname "$SKILL_DST")"
    cp "$SKILL_SRC" "$SKILL_DST"
    echo "  Skill installed: $SKILL_DST"
else
    echo "  WARNING: Skill file not found at $SKILL_SRC"
fi

# 3. Check MCP configuration
echo ""
echo "[3/3] Checking MCP configuration..."
MEMORA_SERVER=$(command -v memora-server 2>/dev/null || echo "")

# Look for existing memora config in common locations
MCP_FOUND=false
for MCP_PATH in \
    "$HOME/.claude/.mcp.json" \
    "$HOME/.mcp.json" \
    ".mcp.json"; do
    if [ -f "$MCP_PATH" ] && grep -q '"memora"' "$MCP_PATH" 2>/dev/null; then
        echo "  Found memora config in: $MCP_PATH"
        MCP_FOUND=true
        break
    fi
done

if [ "$MCP_FOUND" = false ]; then
    echo "  No MCP config found for memora."
    echo ""
    echo "  Add this to your project's .mcp.json:"
    echo ""
    cat <<'EXAMPLE'
  {
    "mcpServers": {
      "memora": {
        "command": "memora-server",
        "args": ["--no-graph"],
        "env": {
          "MEMORA_EMBEDDING_MODEL": "openai",
          "OPENAI_API_KEY": "<openrouter-key>",
          "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
          "MEMORA_LLM_MODEL": "deepseek/deepseek-chat",
          "MEMORA_EMBEDDING_API_KEY": "<cloudflare-api-token-with-workers-ai>",
          "MEMORA_EMBEDDING_BASE_URL": "https://api.cloudflare.com/client/v4/accounts/<account_id>/ai/v1",
          "OPENAI_EMBEDDING_MODEL": "@cf/baai/bge-m3",
          "MEMORA_EMBEDDING_STRICT": "1"
        }
      }
    }
  }
EXAMPLE
    echo ""
    echo "  IMPORTANT: OpenRouter has NO embeddings endpoint. Do not use an openai/"
    echo "  prefixed model for embeddings. The example splits LLM (OpenRouter) from"
    echo "  embeddings (Cloudflare Workers AI @cf/baai/bge-m3, 1024-d). Set BOTH"
    echo "  MEMORA_EMBEDDING_API_KEY and MEMORA_EMBEDDING_BASE_URL (atomic pair)."
    echo "  MEMORA_EMBEDDING_STRICT=1 is recommended so a broken endpoint fails loud"
    echo "  instead of silently filling the store with TF-IDF keyword bags."
    echo "  For cloud storage (D1/R2), add MEMORA_STORAGE_URI and credentials."
    echo "  See README.md (Semantic Search & Embeddings)."
fi

echo ""
echo "=== Done ==="
echo ""
echo "Next steps:"
echo "  1. Configure .mcp.json in your project (if not already done)"
echo "  2. Restart Claude Code to pick up the new server and skill"
echo "  3. Test: /memora or ask Claude to search memories"
