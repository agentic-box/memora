# Memora Plugin for Claude Code

Persistent semantic memory for Claude Code sessions. Automatically injects relevant context at session start, captures significant actions via hooks, and provides 30+ MCP tools for memory management.

## Features

- **Context injection**: SessionStart hook searches memora for relevant memories and injects them into the session
- **Auto-capture**: PostToolUse hook captures git commits, test results, web research, and documentation edits
- **MCP tools**: 30+ tools for creating, searching, organizing, and maintaining memories
- **Knowledge graph**: Interactive visualization at `http://localhost:8765`
- **Semantic search**: Hybrid search combining keyword (FTS) and vector similarity

## Installation

### Prerequisites

Install memora via pip:

```bash
pip install git+https://github.com/agentic-mcp-tools/memora.git
```

Verify the server binary is available:

```bash
memora-server info
```

### Plugin Installation

#### Option 1: Symlink (recommended for development)

```bash
ln -s /path/to/memora/claude-plugin ~/.claude/plugins/memora
```

#### Option 2: Copy

```bash
cp -r /path/to/memora/claude-plugin ~/.claude/plugins/memora
```

#### Option 3: Claude Code marketplace (when available)

```bash
claude plugins add memora
```

### Enable the Plugin

Add to `~/.claude/settings.json` under `enabledPlugins`:

```json
{
  "enabledPlugins": {
    "memora@memora": true
  }
}
```

Restart Claude Code after installation.

## What's Included

| Component         | File                              | Purpose                                    |
| ----------------- | --------------------------------- | ------------------------------------------ |
| Manifest          | `.claude-plugin/plugin.json`      | Plugin metadata and version                |
| MCP Server        | `.mcp.json`                       | Auto-configures memora MCP server          |
| SessionStart Hook | `hooks-handlers/session_start.py` | Injects relevant memories at session start |
| PostToolUse Hook  | `hooks-handlers/post_tool_use.py` | Auto-captures significant actions          |
| Skill             | `skills/memora/SKILL.md`          | Usage guide for memora MCP tools           |

## Configuration

The plugin uses sensible defaults. To customize, set environment variables:

| Variable               | Default                             | Description                             |
| ---------------------- | ----------------------------------- | --------------------------------------- |
| `MEMORA_DB_PATH`       | `~/.local/share/memora/memories.db` | SQLite database location                |
| `MEMORA_ALLOW_ANY_TAG` | `1`                                 | Allow creating tags on the fly          |
| `MEMORA_GRAPH_PORT`    | `8765`                              | Knowledge graph server port             |
| `MEMORA_AUTO_CAPTURE`  | `false`                             | Enable auto-capture in PostToolUse hook |

Override via the `.mcp.json` `env` section or system environment variables.

## How It Works

### SessionStart Hook

1. Loads memora config from plugin `.mcp.json` (falls back to `~/.claude/settings.json`)
2. Extracts project name from working directory
3. Runs hybrid search (keyword + semantic) for top 5 relevant memories
4. Injects formatted memories as `additionalContext` in the session prompt

### PostToolUse Hook (when `MEMORA_AUTO_CAPTURE=true`)

Captures actions with inherent context:

- **Git commits**: Maintained as a single per-project log memory
- **Test results**: Failures automatically create issues
- **Web research**: GitHub repos, documentation, comparison research
- **Documentation edits**: README, CHANGELOG, CONTRIBUTING, etc.

Does **not** capture raw code edits (Edit/Write to source files) since the hook lacks conversation context about _why_ changes were made.

Features:

- Content-hash deduplication with 30-minute TTL cache
- Significance scoring to filter noise
- Automatic hierarchy placement based on existing memory structure
- Finds and updates existing memories instead of creating duplicates

## Portability

All paths use `${CLAUDE_PLUGIN_ROOT}` for portability. The hooks use `python3` from the system PATH — memora must be installed in the default Python environment or a virtualenv that's on your PATH.
