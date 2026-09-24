// The graph SPA's deployment configuration (CFG1): the realtime WebSocket
// URL comes from the WS_WORKER_URL var (the git-ignored wrangler.toml), so
// no account subdomain is in the public index.html. null = no realtime.
interface Env {
  WS_WORKER_URL?: string;
}

export function wsUrlFrom(workerUrl: string | undefined): string | null {
  if (!workerUrl) return null;
  try {
    const u = new URL(workerUrl);
    if (u.protocol !== "https:" && u.protocol !== "http:") return null;
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = u.pathname.replace(/\/+$/, "") + "/ws";
    u.search = "";
    u.hash = "";
    return u.toString();
  } catch {
    return null;
  }
}

export const onRequestGet: PagesFunction<Env> = async ({ env }) =>
  Response.json({ wsUrl: wsUrlFrom(env.WS_WORKER_URL) });
