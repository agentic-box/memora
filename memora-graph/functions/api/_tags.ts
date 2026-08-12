export const TAG_POLICY_KEY = "tag_policy_v1";

export interface TagPolicy {
  version: 1;
  allow_any: boolean;
  tags: string[];
}

export type TagPolicyResult =
  | { ok: true; policy: TagPolicy }
  | { ok: false; error: "tag_policy_unavailable" };

export type TagValidationResult =
  | { ok: true; tags: string[] }
  | { ok: false; error: "invalid_tags"; message: string };

export async function loadTagPolicy(db: D1Database): Promise<TagPolicyResult> {
  try {
    const row = await db.prepare(
      "SELECT value FROM memories_meta WHERE key = ?"
    ).bind(TAG_POLICY_KEY).first<{ value: string }>();
    if (!row || typeof row.value !== "string") return { ok: false, error: "tag_policy_unavailable" };
    const parsed: unknown = JSON.parse(row.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ok: false, error: "tag_policy_unavailable" };
    }
    const candidate = parsed as Record<string, unknown>;
    if (
      candidate.version !== 1
      || typeof candidate.allow_any !== "boolean"
      || !Array.isArray(candidate.tags)
      || candidate.tags.some(tag => typeof tag !== "string" || tag.trim() !== tag || !tag)
    ) {
      return { ok: false, error: "tag_policy_unavailable" };
    }
    return {
      ok: true,
      policy: {
        version: 1,
        allow_any: candidate.allow_any,
        tags: candidate.tags as string[],
      },
    };
  } catch {
    return { ok: false, error: "tag_policy_unavailable" };
  }
}

export const MAX_TAG_LENGTH = 100;

/** Separator-specific wildcard: prefix.* / prefix/* ; no bare-* catch-all. */
export function tagMatchesPattern(tag: string, pattern: string): boolean {
  if (pattern.endsWith(".*")) {
    const prefix = pattern.slice(0, -2);
    if (!prefix) return false;
    return tag === prefix || tag.startsWith(prefix + ".");
  }
  if (pattern.endsWith("/*")) {
    const prefix = pattern.slice(0, -2);
    if (!prefix) return false;
    return tag === prefix || tag.startsWith(prefix + "/");
  }
  if (pattern === "*") return false;
  return tag === pattern;
}

export function tagMatchesPolicy(tag: string, policyTags: string[]): boolean {
  return policyTags.some(pattern => tagMatchesPattern(tag, pattern));
}

export function validateTags(value: unknown, policy: TagPolicy): TagValidationResult {
  if (!Array.isArray(value)) {
    return { ok: false, error: "invalid_tags", message: "Tags must be an array." };
  }
  const tags: string[] = [];
  for (const raw of value) {
    if (typeof raw !== "string") {
      return { ok: false, error: "invalid_tags", message: "Tags must be strings." };
    }
    const tag = raw.trim();
    if (!tag) {
      return { ok: false, error: "invalid_tags", message: "Tags cannot be empty strings." };
    }
    if (tag.length > MAX_TAG_LENGTH) {
      return {
        ok: false,
        error: "invalid_tags",
        message: `Tag exceeds maximum length of ${MAX_TAG_LENGTH} characters`,
      };
    }
    tags.push(tag);
  }
  if (policy.allow_any) return { ok: true, tags };

  for (const tag of tags) {
    if (tagMatchesPolicy(tag, policy.tags)) continue;
    return {
      ok: false,
      error: "invalid_tags",
      message: `Tag '${tag}' is not in the allowed tag list.`,
    };
  }
  return { ok: true, tags };
}

export function tagPolicyUnavailableResponse(): Response {
  return Response.json({ error: "tag_policy_unavailable" }, { status: 503 });
}
