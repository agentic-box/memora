export interface DatabaseEnv {
  DEFAULT_DB?: string;
  DB_CONFIG?: string;
  [binding: string]: unknown;
}

export interface DatabaseCatalog {
  databases: string[];
  defaultDatabase: string;
}

export type BindingSelection<T> =
  | { ok: true; name: string; binding: T }
  | { ok: false; status: 400 | 500; error: "unknown_database" | "database_binding_missing"; name: string };

const FALLBACK_CONFIG: Readonly<Record<string, string>> = {
  memora: "MEMORA",
  ob1: "OB1",
};

const BINDING_SUFFIX = /^[A-Z][A-Z0-9_]*$/;

function fallbackConfig(): Record<string, string> {
  return { ...FALLBACK_CONFIG };
}

export function databaseConfig(env: DatabaseEnv): Record<string, string> {
  if (!env.DB_CONFIG) return fallbackConfig();

  try {
    const parsed: unknown = JSON.parse(env.DB_CONFIG);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return fallbackConfig();
    }

    const entries = Object.entries(parsed).filter(
      ([name, suffix]) => name.length > 0 && typeof suffix === "string" && BINDING_SUFFIX.test(suffix),
    ) as Array<[string, string]>;
    return entries.length > 0 ? Object.fromEntries(entries) : fallbackConfig();
  } catch {
    return fallbackConfig();
  }
}

export function databaseCatalog(env: DatabaseEnv): DatabaseCatalog {
  const config = databaseConfig(env);
  const databases = Object.keys(config);
  const defaultDatabase = env.DEFAULT_DB && Object.hasOwn(config, env.DEFAULT_DB)
    ? env.DEFAULT_DB
    : databases[0];
  return { databases, defaultDatabase };
}

function resolveBinding<T>(
  env: DatabaseEnv,
  requestedName: string | null,
  prefix: "DB_" | "R2_",
): BindingSelection<T> {
  const config = databaseConfig(env);
  const databases = Object.keys(config);
  const fallbackName = env.DEFAULT_DB && Object.hasOwn(config, env.DEFAULT_DB)
    ? env.DEFAULT_DB
    : databases[0];
  const name = requestedName || fallbackName;

  if (!Object.hasOwn(config, name)) {
    return { ok: false, status: 400, error: "unknown_database", name };
  }

  const binding = env[prefix + config[name]];
  if (!binding) {
    return { ok: false, status: 500, error: "database_binding_missing", name };
  }
  return { ok: true, name, binding: binding as T };
}

export function resolveDatabase(env: DatabaseEnv, requestedName: string | null): BindingSelection<D1Database> {
  return resolveBinding<D1Database>(env, requestedName, "DB_");
}

export function resolveBucket(env: DatabaseEnv, requestedName: string | null): BindingSelection<R2Bucket> {
  return resolveBinding<R2Bucket>(env, requestedName, "R2_");
}

export function selectionErrorResponse(selection: Extract<BindingSelection<unknown>, { ok: false }>): Response {
  return Response.json(
    { error: selection.error, database: selection.name },
    { status: selection.status },
  );
}
