/**
 * Memora Graph Sync Worker
 *
 * This worker handles real-time WebSocket connections for the graph UI.
 * It uses a Durable Object to manage connections and broadcast updates.
 */

export interface Env {
  GRAPH_SYNC: DurableObjectNamespace;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // CORS headers
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-Sync-Secret",
    };

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // Get the singleton Durable Object
    const id = env.GRAPH_SYNC.idFromName("singleton");
    const stub = env.GRAPH_SYNC.get(id);

    // Route requests
    if (url.pathname === "/ws" || url.pathname === "/") {
      // WebSocket upgrade
      if (request.headers.get("Upgrade") === "websocket") {
        return stub.fetch(request);
      }
      return new Response("WebSocket endpoint. Connect with ws:// or wss://", {
        headers: corsHeaders,
      });
    }

    if (url.pathname === "/broadcast") {
      // Broadcast to all connected clients
      const response = await stub.fetch(new Request("https://internal/broadcast", {
        method: "POST",
        headers: request.headers,
        body: request.body,
      }));
      const data = await response.json();
      return Response.json(data, { headers: corsHeaders });
    }

    if (url.pathname === "/health") {
      const response = await stub.fetch("https://internal/health");
      const data = await response.json();
      return Response.json(data, { headers: corsHeaders });
    }

    return new Response("Not found", { status: 404, headers: corsHeaders });
  },
};

/**
 * Durable Object for managing WebSocket connections
 */
export class GraphSyncDO implements DurableObject {
  private connections: Set<WebSocket> = new Set();
  private state: DurableObjectState;

  constructor(state: DurableObjectState) {
    this.state = state;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    // WebSocket upgrade request
    if (request.headers.get("Upgrade") === "websocket") {
      return this.handleWebSocket();
    }

    // Broadcast notification
    if (url.pathname === "/broadcast" && request.method === "POST") {
      return this.handleBroadcast(request);
    }

    // Health check
    if (url.pathname === "/health") {
      return Response.json({
        status: "ok",
        connections: this.connections.size,
      });
    }

    return new Response("Not found", { status: 404 });
  }

  private handleWebSocket(): Response {
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    // Accept the WebSocket
    server.accept();
    this.connections.add(server);

    // Send initial confirmation
    server.send(JSON.stringify({
      type: "connected",
      timestamp: new Date().toISOString(),
      connections: this.connections.size,
    }));

    // Handle messages (no-op; kept for future use)
    server.addEventListener("message", () => {});

    // Handle close
    server.addEventListener("close", () => {
      this.connections.delete(server);
    });

    // Handle error
    server.addEventListener("error", () => {
      this.connections.delete(server);
    });

    return new Response(null, {
      status: 101,
      webSocket: client,
    });
  }

  private async handleBroadcast(request: Request): Promise<Response> {
    let data: Record<string, unknown> = {};
    try {
      data = await request.json() as Record<string, unknown>;
    } catch {
      // Empty body is fine
    }

    const message = JSON.stringify({
      type: "graph_updated",
      timestamp: new Date().toISOString(),
      ...data,
    });

    let sent = 0;
    let closed = 0;

    for (const ws of this.connections) {
      try {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(message);
          sent++;
        } else {
          this.connections.delete(ws);
          closed++;
        }
      } catch {
        this.connections.delete(ws);
        closed++;
      }
    }

    return Response.json({
      status: "broadcast_sent",
      sent,
      closed,
      remaining: this.connections.size,
    });
  }
}
