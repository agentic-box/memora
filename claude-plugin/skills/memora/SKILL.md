---
name: memora
description: Use when working with persistent memory across sessions, storing/retrieving knowledge, managing TODOs/issues, or when context from previous sessions would be helpful.
version: 0.2.20
---

# Memora - Persistent Semantic Memory

Memora is the persistent memory system for this environment. Use memora MCP tools to store, search, and organize knowledge across sessions.

## When to Use

- **Session start**: Relevant memories are auto-injected via hook
- **Storing decisions**: Use `memory_create` to save architectural decisions, patterns, preferences
- **Finding context**: Use `memory_hybrid_search` to find relevant past work
- **Tracking work**: Use `memory_create_todo` / `memory_create_issue` for task tracking
- **Organizing knowledge**: Use `memory_hierarchy` to browse organized memories

## Core Tools

### Creating Memories

- `memory_create` - Store a new memory (auto-deduplicates, suggests hierarchy)
- `memory_create_todo` - Create a TODO with priority (high/medium/low)
- `memory_create_issue` - Create an issue with severity (critical/major/minor)
- `memory_create_section` - Create organizational headers
- `memory_create_batch` - Bulk create multiple memories

### Searching

- `memory_hybrid_search` - Best search: combines keyword + semantic (use this by default)
- `memory_semantic_search` - Pure vector similarity search
- `memory_list` - List/filter by tags, dates, metadata
- `memory_list_compact` - Lightweight listing (id, preview, tags only)

### Organizing

- `memory_hierarchy` - View memories in section/subsection tree
- `memory_tags` - List allowed tags
- `memory_tag_hierarchy` - View tag namespace tree
- `memory_link` - Create typed relationships between memories
- `memory_clusters` - Detect related memory clusters

### Maintenance

- `memory_find_duplicates` - Find and review potential duplicates (LLM-powered)
- `memory_merge` - Merge two memories together
- `memory_insights` - Get activity summary, stale items, patterns
- `memory_stats` - Database statistics
- `memory_boost` - Increase a memory's importance ranking

### Visualization

- Knowledge graph available at http://localhost:8765 when running
- `memory_export_graph` - Export as interactive HTML file

## Tag Conventions

Use hierarchical tags with `/` separators:

- `memora/knowledge` - General knowledge
- `memora/todos` - Task items
- `memora/issues` - Bug/issue tracking
- `memora/auto-capture` - Auto-captured content
- `memora/sections` - Organizational headers
- `project-name/topic` - Project-specific tags

## Best Practices

1. **Search before creating** - avoid duplicates
2. **Use metadata** for structured data (`section`, `subsection`, `project`)
3. **Tag consistently** - use hierarchical tags
4. **Boost important memories** - they rank higher in searches
5. **Use hybrid search** as default - it combines keyword + semantic
6. **Review insights periodically** - find stale items and consolidation opportunities

## Auto-Capture

When `MEMORA_AUTO_CAPTURE=true` is set, the PostToolUse hook automatically captures:

- Git commits (appended to per-project commit log)
- Test results (failures become issues)
- Web research (GitHub repos, documentation)
- Documentation edits (README, CHANGELOG, etc.)
