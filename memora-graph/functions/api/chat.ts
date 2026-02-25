/**
 * POST /api/chat - Chat about memories using LLM with RAG + tool calling
 * Uses semantic search (embeddings) + keyword search for memory retrieval.
 * Supports create/update/delete memories via OpenAI-style tool calling.
 * Requires OPENROUTER_API_KEY secret and optionally CHAT_MODEL env var.
 * Supports ?db=memora or ?db=ob1 parameter to select database.
 */

interface Env {
  DB_MEMORA: D1Database;
  DB_OB1: D1Database;
  DEFAULT_DB?: string;
  OPENROUTER_API_KEY?: string;
  CHAT_MODEL?: string;
  EMBEDDING_MODEL?: string;
  REWRITE_MODEL?: string;
}

function getDatabase(env: Env, dbName: string | null): D1Database {
  const name = dbName || env.DEFAULT_DB || "memora";
  if (name === "ob1") return env.DB_OB1;
  return env.DB_MEMORA;
}

interface MemoryRow {
  id: number;
  content: string;
  tags: string;
  created_at?: string;
}

interface ChatMessage {
  role: string;
  content: string | null;
  tool_calls?: Array<{
    id: string;
    type: string;
    function: { name: string; arguments: string };
  }>;
  tool_call_id?: string;
}

interface ChatRequest {
  message: string;
  history?: ChatMessage[];
}

interface MemoryReference {
  id: number;
  score: number;
  preview: string;
  method?: string;
}

function parseJson<T>(str: string | null, defaultValue: T): T {
  if (!str) return defaultValue;
  try {
    return JSON.parse(str);
  } catch {
    return defaultValue;
  }
}

// ── Tool definitions ──────────────────────────────────────────────────

const CHAT_TOOLS = [
  {
    type: "function" as const,
    function: {
      name: "create_memory",
      description:
        "Create a new memory in the knowledge base. Use when the user asks to save, create, add, or remember something.",
      parameters: {
        type: "object",
        properties: {
          content: {
            type: "string",
            description: "The full text content of the memory.",
          },
          tags: {
            type: "array",
            items: { type: "string" },
            description: "Optional tags to categorize the memory.",
          },
        },
        required: ["content"],
      },
    },
  },
  {
    type: "function" as const,
    function: {
      name: "update_memory",
      description:
        "Update an existing memory by ID. Use when the user asks to modify, edit, or change a specific memory.",
      parameters: {
        type: "object",
        properties: {
          memory_id: {
            type: "integer",
            description: "The ID of the memory to update.",
          },
          content: {
            type: "string",
            description: "New full text content. Replaces existing content.",
          },
          tags: {
            type: "array",
            items: { type: "string" },
            description: "New tags. Replaces all existing tags.",
          },
        },
        required: ["memory_id"],
      },
    },
  },
  {
    type: "function" as const,
    function: {
      name: "delete_memory",
      description:
        "Delete a memory by ID. Use when the user asks to remove or delete a specific memory.",
      parameters: {
        type: "object",
        properties: {
          memory_id: {
            type: "integer",
            description: "The ID of the memory to delete.",
          },
        },
        required: ["memory_id"],
      },
    },
  },
];

// ── Tool execution via D1 ─────────────────────────────────────────────

async function executeToolCall(
  db: D1Database,
  toolName: string,
  args: Record<string, unknown>
): Promise<string> {
  try {
    if (toolName === "create_memory") {
      const content = String(args.content || "");
      const tags = Array.isArray(args.tags) ? args.tags : [];
      const result = await db
        .prepare(
          "INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, '{}', ?, datetime('now'))"
        )
        .bind(content, JSON.stringify(tags))
        .run();
      const newId = result.meta?.last_row_id;
      return JSON.stringify({
        success: true,
        action: "created",
        memory_id: newId,
        preview: content.slice(0, 100),
      });
    }

    if (toolName === "update_memory") {
      const mid = Number(args.memory_id);
      if (!mid || isNaN(mid)) {
        return JSON.stringify({ success: false, error: "Invalid memory_id." });
      }
      // Fetch existing
      const existing = await db
        .prepare("SELECT id, content, tags FROM memories WHERE id = ?")
        .bind(mid)
        .first<MemoryRow>();
      if (!existing) {
        return JSON.stringify({
          success: false,
          error: `Memory #${mid} not found.`,
        });
      }
      const newContent =
        args.content !== undefined ? String(args.content) : existing.content;
      const newTags =
        args.tags !== undefined
          ? JSON.stringify(args.tags)
          : existing.tags;
      await db
        .prepare(
          "UPDATE memories SET content = ?, tags = ?, updated_at = datetime('now') WHERE id = ?"
        )
        .bind(newContent, newTags, mid)
        .run();
      return JSON.stringify({
        success: true,
        action: "updated",
        memory_id: mid,
        preview: newContent.slice(0, 100),
      });
    }

    if (toolName === "delete_memory") {
      const mid = Number(args.memory_id);
      if (!mid || isNaN(mid)) {
        return JSON.stringify({ success: false, error: "Invalid memory_id." });
      }
      const existing = await db
        .prepare("SELECT id, content FROM memories WHERE id = ?")
        .bind(mid)
        .first<{ id: number; content: string }>();
      if (!existing) {
        return JSON.stringify({
          success: false,
          error: `Memory #${mid} not found.`,
        });
      }
      // Delete related data first, then the memory
      await db.batch([
        db.prepare("DELETE FROM memories_embeddings WHERE memory_id = ?").bind(mid),
        db.prepare("DELETE FROM memories_crossrefs WHERE memory_id = ?").bind(mid),
        db.prepare("DELETE FROM memories WHERE id = ?").bind(mid),
      ]);
      return JSON.stringify({
        success: true,
        action: "deleted",
        memory_id: mid,
        preview: existing.content.slice(0, 100),
      });
    }

    return JSON.stringify({ success: false, error: `Unknown tool: ${toolName}` });
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    return JSON.stringify({ success: false, error: msg.slice(0, 200) });
  }
}

// ── Embedding / search helpers ────────────────────────────────────────

async function getQueryEmbedding(
  query: string,
  apiKey: string,
  model: string
): Promise<number[] | null> {
  try {
    const response = await fetch(
      "https://openrouter.ai/api/v1/embeddings",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ input: query, model }),
      }
    );
    if (!response.ok) return null;
    const data = await response.json<{
      data: Array<{ embedding: number[] }>;
    }>();
    return data.data?.[0]?.embedding || null;
  } catch {
    return null;
  }
}

function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length !== b.length || a.length === 0) return 0;
  let dot = 0,
    normA = 0,
    normB = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }
  const denom = Math.sqrt(normA) * Math.sqrt(normB);
  return denom === 0 ? 0 : dot / denom;
}

async function semanticSearch(
  db: D1Database,
  queryEmbedding: number[],
  topK: number
): Promise<Array<{ memory: MemoryRow; score: number }>> {
  const result = await db
    .prepare(
      `SELECT m.id, m.content, m.tags, m.created_at, e.embedding
       FROM memories m
       JOIN memories_embeddings e ON e.memory_id = m.id`
    )
    .all<MemoryRow & { embedding: string }>();

  if (!result.results || result.results.length === 0) return [];

  const now = Date.now();
  function recencyBoost(createdAt: string | undefined): number {
    if (!createdAt) return 0;
    const age = now - new Date(createdAt).getTime();
    const days = age / (1000 * 60 * 60 * 24);
    return Math.max(0, 0.05 * (1 - days / 90));
  }

  const scored: Array<{ memory: MemoryRow; score: number }> = [];
  for (const row of result.results) {
    const pairs = parseJson<Array<[string, number]>>(row.embedding, []);
    if (pairs.length === 0) continue;
    const dense = new Array(queryEmbedding.length).fill(0);
    for (const [k, v] of pairs) {
      const idx = parseInt(k, 10);
      if (idx < dense.length) dense[idx] = v;
    }
    const similarity = cosineSimilarity(queryEmbedding, dense);
    const boost = recencyBoost(row.created_at);
    if (similarity > 0.1) {
      scored.push({
        memory: {
          id: row.id,
          content: row.content,
          tags: row.tags,
          created_at: row.created_at,
        },
        score: similarity + boost,
      });
    }
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, topK);
}

async function keywordSearch(
  db: D1Database,
  query: string,
  topK: number
): Promise<Array<{ memory: MemoryRow; score: number }>> {
  const keywords = query
    .toLowerCase()
    .split(/\s+/)
    .map((w) => w.replace(/[^a-z0-9-]/g, ""))
    .filter((w) => w.length >= 3);

  if (keywords.length === 0) {
    const result = await db
      .prepare(
        "SELECT id, content, tags FROM memories ORDER BY created_at DESC LIMIT ?"
      )
      .bind(topK)
      .all<MemoryRow>();
    return (result.results || []).map((m) => ({ memory: m, score: 0.1 }));
  }

  const conditions = keywords.map(
    () => "(LOWER(content) LIKE ? OR LOWER(tags) LIKE ?)"
  );
  const params: string[] = [];
  for (const k of keywords) {
    params.push(`%${k}%`, `%${k}%`);
  }

  const sql = `
    SELECT id, content, tags
    FROM memories
    WHERE ${conditions.join(" OR ")}
    ORDER BY created_at DESC
    LIMIT ?
  `;

  const result = await db
    .prepare(sql)
    .bind(...params, topK)
    .all<MemoryRow>();

  return (result.results || []).map((m) => ({
    memory: { id: m.id, content: m.content, tags: m.tags },
    score: 0.3,
  }));
}

async function searchMemories(
  db: D1Database,
  query: string,
  apiKey: string,
  embeddingModel: string,
  topK: number = 8
): Promise<{
  results: Array<{ memory: MemoryRow; score: number }>;
  method: string;
}> {
  const queryEmbedding = await getQueryEmbedding(query, apiKey, embeddingModel);
  if (queryEmbedding) {
    const results = await semanticSearch(db, queryEmbedding, topK);
    if (results.length > 0) return { results, method: "semantic" };
  }
  const kwResults = await keywordSearch(db, query, topK);
  if (kwResults.length > 0) return { results: kwResults, method: "keyword" };
  const result = await db
    .prepare(
      "SELECT id, content, tags FROM memories ORDER BY created_at DESC LIMIT ?"
    )
    .bind(topK)
    .all<MemoryRow>();
  return {
    results: (result.results || []).map((m) => ({ memory: m, score: 0.1 })),
    method: "recent",
  };
}

// ── Query rewriting for improved RAG ──────────────────────────────────

interface RewriteResult {
  queries: string[];
  filters: {
    date_from?: string | null;
    date_to?: string | null;
    tags_any?: string[] | null;
  };
}

async function rewriteQuery(
  message: string,
  apiKey: string,
  model: string
): Promise<RewriteResult> {
  const fallback: RewriteResult = { queries: [message], filters: {} };

  const today = new Date().toISOString().split("T")[0];

  const systemPrompt =
    "You are a search query optimizer for a personal knowledge base. " +
    "Given a user's question, generate 1-3 search queries that would find relevant memories.\n\n" +
    "Rules:\n" +
    "- Generate diverse queries: rephrase, use synonyms, extract key entities\n" +
    "- If the user message is already a simple search query, return just that query\n" +
    "- If the message contains a time reference, extract it as date_from/date_to in ISO format (YYYY-MM-DD)\n" +
    "- If the message references categories/types, extract relevant tags into tags_any\n" +
    "- Keep queries concise (under 15 words each)\n" +
    "- For conversational/meta messages, return the original as a single query\n\n" +
    "Respond with JSON only (no markdown fences):\n" +
    '{"queries": ["q1", "q2"], "filters": {"date_from": null, "date_to": null, "tags_any": null}}';

  try {
    const response = await fetch(
      "https://openrouter.ai/api/v1/chat/completions",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model,
          messages: [
            { role: "system", content: systemPrompt },
            {
              role: "user",
              content: `User message: "${message}"\nToday's date: ${today}`,
            },
          ],
          temperature: 0.3,
          max_tokens: 300,
          stream: false,
        }),
      }
    );

    if (!response.ok) return fallback;

    const data = await response.json<{
      choices: Array<{ message: { content: string } }>;
    }>();
    let text = data.choices?.[0]?.message?.content?.trim() || "";

    // Strip markdown fences
    if (text.startsWith("```")) {
      text = text.split("\n").slice(1).join("\n").replace(/```\s*$/, "").trim();
    }

    const parsed = JSON.parse(text) as RewriteResult;

    const queries = (parsed.queries || [])
      .filter((q): q is string => typeof q === "string" && q.trim().length > 0)
      .slice(0, 3);

    if (queries.length === 0) return fallback;

    return { queries, filters: parsed.filters || {} };
  } catch {
    return fallback;
  }
}

async function multiQuerySearch(
  db: D1Database,
  queries: string[],
  apiKey: string,
  embeddingModel: string,
  topK: number = 8
): Promise<{
  results: Array<{ memory: MemoryRow; score: number }>;
  method: string;
}> {
  if (queries.length === 0) {
    return { results: [], method: "none" };
  }

  // Run searches in parallel
  const searchPromises = queries.map((q) =>
    searchMemories(db, q, apiKey, embeddingModel, topK)
  );
  const searchResults = await Promise.all(searchPromises);

  // Second-level RRF fusion across query results
  const rrfK = 60;
  const scores = new Map<number, number>();
  const memoriesById = new Map<number, MemoryRow>();
  let method = "multi_query";

  for (const { results, method: m } of searchResults) {
    if (method === "multi_query") method = `multi_query_${m}`;
    for (let rank = 0; rank < results.length; rank++) {
      const mem = results[rank].memory;
      memoriesById.set(mem.id, mem);
      const prev = scores.get(mem.id) || 0;
      scores.set(mem.id, prev + 1 / (rrfK + rank));
    }
  }

  // Sort by fused score, take top-K
  const sorted = [...scores.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, topK);

  const fusedResults = sorted.map(([id, score]) => ({
    memory: memoriesById.get(id)!,
    score: Math.round(score * 1000) / 1000,
  }));

  return { results: fusedResults, method };
}

// ── LLM call helper ───────────────────────────────────────────────────

interface LLMCallOptions {
  apiKey: string;
  model: string;
  messages: ChatMessage[];
  origin: string;
  tools?: typeof CHAT_TOOLS;
}

async function callLLM(opts: LLMCallOptions): Promise<Response> {
  const body: Record<string, unknown> = {
    model: opts.model,
    messages: opts.messages,
    stream: true,
    temperature: 0.7,
    max_tokens: 2000,
  };
  if (opts.tools) {
    body.tools = opts.tools;
  }
  return fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${opts.apiKey}`,
      "Content-Type": "application/json",
      "HTTP-Referer": opts.origin,
    },
    body: JSON.stringify(body),
  });
}

// ── Parse one SSE stream, accumulating content + tool_call deltas ─────

interface StreamResult {
  content: string;
  toolCalls: Record<
    number,
    { id: string; name: string; arguments: string }
  >;
}

async function consumeStream(
  response: Response,
  onToken: (text: string) => Promise<void>
): Promise<StreamResult> {
  const decoder = new TextDecoder();
  let buffer = "";
  const result: StreamResult = { content: "", toolCalls: {} };

  const reader = response.body!.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (data === "[DONE]") continue;

      try {
        const chunk = JSON.parse(data);
        const delta = chunk.choices?.[0]?.delta;
        if (!delta) continue;

        // Content tokens
        if (delta.content) {
          result.content += delta.content;
          await onToken(delta.content);
        }

        // Tool call deltas
        if (delta.tool_calls) {
          for (const tc of delta.tool_calls) {
            const idx = tc.index ?? 0;
            if (!result.toolCalls[idx]) {
              result.toolCalls[idx] = { id: "", name: "", arguments: "" };
            }
            const entry = result.toolCalls[idx];
            if (tc.id) entry.id = tc.id;
            if (tc.function?.name) entry.name = tc.function.name;
            if (tc.function?.arguments)
              entry.arguments += tc.function.arguments;
          }
        }
      } catch {
        // Skip malformed chunks
      }
    }
  }

  return result;
}

// ── Main handler ──────────────────────────────────────────────────────

export const onRequestPost: PagesFunction<Env> = async ({
  env,
  request,
}) => {
  const url = new URL(request.url);
  const dbName = url.searchParams.get("db");
  const db = getDatabase(env, dbName);

  const apiKey = env.OPENROUTER_API_KEY;
  if (!apiKey) {
    return Response.json(
      {
        error: "llm_not_configured",
        message:
          "LLM not configured. Set OPENROUTER_API_KEY secret in Cloudflare dashboard.",
      },
      { status: 503 }
    );
  }

  let body: ChatRequest;
  try {
    body = await request.json<ChatRequest>();
  } catch {
    return Response.json({ error: "invalid_json" }, { status: 400 });
  }

  const message = (body.message || "").trim();
  if (!message) {
    return Response.json({ error: "empty_message" }, { status: 400 });
  }

  const history = body.history || [];
  const model = env.CHAT_MODEL || "deepseek/deepseek-chat";
  const embeddingModel =
    env.EMBEDDING_MODEL || "openai/text-embedding-3-small";
  const origin = url.origin;

  // Rewrite query for improved retrieval, then multi-query search
  const rewriteModel =
    env.REWRITE_MODEL || env.CHAT_MODEL || "deepseek/deepseek-chat";
  const rewriteResult = await rewriteQuery(message, apiKey, rewriteModel);

  const { results: searchResults, method: searchMethod } =
    await multiQuerySearch(
      db,
      rewriteResult.queries,
      apiKey,
      embeddingModel,
      8
    );

  // Build references and context
  const references: MemoryReference[] = [];
  const contextParts: string[] = [];

  for (const r of searchResults) {
    const mem = r.memory;
    const tags = parseJson<string[]>(mem.tags, []);
    references.push({
      id: mem.id,
      score: Math.round(r.score * 1000) / 1000,
      preview: mem.content.slice(0, 100).replace(/\n/g, " "),
    });
    const tagsStr = tags.join(", ");
    const dateStr = mem.created_at
      ? ` [${mem.created_at.split(" ")[0]}]`
      : "";
    const contentTruncated = mem.content.slice(0, 1500);
    contextParts.push(
      `Memory #${mem.id} (tags: ${tagsStr})${dateStr}:\n${contentTruncated}`
    );
  }

  const contextBlock =
    contextParts.length > 0
      ? contextParts.join("\n\n---\n\n")
      : "No relevant memories found.";

  const systemMsg: ChatMessage = {
    role: "system",
    content: [
      "You are a helpful assistant for the user's personal knowledge base (Memora).",
      "Use the following memories as context. When referencing a memory, cite it as [Memory #<id>].",
      "If the memories don't contain relevant information, say so honestly.",
      "",
      "## Tool Use — IMPORTANT",
      "",
      "You have tools to create, update, and delete memories. You MUST call the appropriate tool when the user asks to:",
      "- Create/save/add/remember something → call create_memory",
      "- Update/edit/modify a memory → call update_memory",
      "- Delete/remove a memory → call delete_memory",
      "",
      "ALWAYS call the tool directly. Do NOT ask for confirmation, do NOT say you can't find the memory, do NOT suggest content without calling the tool.",
      "The memory database has many more entries than what's shown in context below — if the user references a memory ID, trust them and call the tool.",
      "When creating a memory, write substantive, well-structured content.",
      "When updating, apply the user's requested changes to the existing content.",
      "",
      "## Relevant Memories",
      "",
      contextBlock,
    ].join("\n"),
  };

  const trimmedHistory = history.slice(-20);
  const messages: ChatMessage[] = [
    systemMsg,
    ...trimmedHistory,
    { role: "user", content: message },
  ];

  // ── Streaming response with tool calling ──────────────────────────

  const encoder = new TextEncoder();
  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();

  const writeSSE = async (event: string, data: string) => {
    const encoded = event === "token" ? JSON.stringify(data) : data;
    await writer.write(
      encoder.encode(`event: ${event}\ndata: ${encoded}\n\n`)
    );
  };

  const processStream = async () => {
    try {
      // Emit references
      if (references.length > 0) references[0].method = searchMethod;
      await writeSSE("references", JSON.stringify(references));

      // First LLM call — with tools
      const llmResponse = await callLLM({
        apiKey,
        model,
        messages,
        origin,
        tools: CHAT_TOOLS,
      });

      if (!llmResponse.ok || !llmResponse.body) {
        const errText = await llmResponse
          .text()
          .catch(() => "Unknown error");
        await writeSSE("error", errText.slice(0, 200));
        return;
      }

      // Consume stream, forwarding content tokens
      const streamResult = await consumeStream(
        llmResponse,
        async (token) => {
          await writeSSE("token", token);
        }
      );

      // No tool calls → done
      const tcIndices = Object.keys(streamResult.toolCalls).map(Number);
      if (tcIndices.length === 0) {
        await writeSSE("done", "");
        return;
      }

      // Execute tool calls
      const toolResults: ChatMessage[] = [];
      for (const idx of tcIndices.sort((a, b) => a - b)) {
        const tc = streamResult.toolCalls[idx];
        let args: Record<string, unknown> = {};
        try {
          args = JSON.parse(tc.arguments);
        } catch {
          /* empty args */
        }

        const resultStr = await executeToolCall(db, tc.name, args);

        // Emit action event to frontend
        const actionData = JSON.parse(resultStr);
        actionData.tool = tc.name;
        await writeSSE("action", JSON.stringify(actionData));

        toolResults.push({
          role: "tool",
          tool_call_id: tc.id,
          content: resultStr,
        });
      }

      // Build assistant message with tool_calls for context
      const assistantMsg: ChatMessage = {
        role: "assistant",
        content: streamResult.content || null,
        tool_calls: tcIndices
          .sort((a, b) => a - b)
          .map((i) => ({
            id: streamResult.toolCalls[i].id,
            type: "function",
            function: {
              name: streamResult.toolCalls[i].name,
              arguments: streamResult.toolCalls[i].arguments,
            },
          })),
      };

      // Second LLM call — with tool results, no tools (prevent loops)
      const llmResponse2 = await callLLM({
        apiKey,
        model,
        messages: [...messages, assistantMsg, ...toolResults],
        origin,
        // no tools on second call
      });

      if (!llmResponse2.ok || !llmResponse2.body) {
        const errText = await llmResponse2
          .text()
          .catch(() => "Unknown error");
        await writeSSE("error", errText.slice(0, 200));
        return;
      }

      // Stream second response content
      await consumeStream(llmResponse2, async (token) => {
        await writeSSE("token", token);
      });

      await writeSSE("done", "");
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : String(e);
      await writeSSE("error", errMsg.slice(0, 200));
    } finally {
      await writer.close();
    }
  };

  // Start processing (runs concurrently with response streaming)
  processStream();

  return new Response(readable, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
};
