DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS memories_crossrefs;
DROP TABLE IF EXISTS memories_meta;

CREATE TABLE memories (
  id INTEGER PRIMARY KEY,
  content TEXT,
  metadata TEXT,
  tags TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE memories_crossrefs (
  memory_id INTEGER PRIMARY KEY,
  related TEXT
);

CREATE TABLE memories_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

INSERT INTO memories_meta (key, value) VALUES
 ('tag_policy_v1', '{"version":1,"allow_any":false,"tags":["deploy","api","team","project.*"]}');

-- 1 <- 2 <- 3 : a three-link supersession chain.
--   #3 is current. #2 is mid-chain (supersedes 1, superseded by 3). #1 is dead.
-- #4 / #5 are ordinary current memories with a NON-lineage typed edge between
--   them (references + contradicts) — these must NOT render as lineage (M1).
-- #6 is superseded, but only the MIRROR half exists (its superseder #7's row
--   carries no supersedes entry) — the M3 drift case.
-- #8 / #9 are a cosine-duplicate pair (related_to, score >= 0.85) so the
--   timeline DUPLICATED filter has a canonical graph.duplicateIds hit.
INSERT INTO memories (id, content, metadata, tags, created_at, updated_at) VALUES
 (1, 'Deploys run from the release branch every Friday.', '{"type":"fact"}', '["deploy"]', '2026-01-05T10:00:00Z', '2026-01-05T10:00:00Z'),
 (2, 'Deploys run from main every Friday.',              '{"type":"fact"}', '["deploy"]', '2026-02-05T10:00:00Z', '2026-02-05T10:00:00Z'),
 (3, 'Deploys run from main on demand, not on a schedule.', '{"type":"fact"}', '["deploy"]', '2026-03-05T10:00:00Z', '2026-03-05T10:00:00Z'),
 (4, 'The graph API caps results at 200 per page.',      '{"type":"fact"}', '["api"]',    '2026-04-05T10:00:00Z', '2026-04-05T10:00:00Z'),
 (5, 'The graph API returns every memory in one request.', '{"type":"fact"}', '["api"]',  '2026-04-06T10:00:00Z', '2026-04-06T10:00:00Z'),
 (6, 'Reviewers may commit fixes directly.',             '{"type":"fact"}', '["team"]',   '2026-05-05T10:00:00Z', '2026-05-05T10:00:00Z'),
 (7, 'Reviews are read-only; the leader applies fixes.', '{"type":"fact"}', '["team"]',   '2026-06-05T10:00:00Z', '2026-06-05T10:00:00Z'),
 (8, 'The cache TTL is fifteen minutes in production.',  '{"type":"fact"}', '["api"]',    '2026-07-05T10:00:00Z', '2026-07-05T10:00:00Z'),
 (9, 'The cache TTL is 15 minutes in production.',       '{"type":"fact"}', '["api"]',    '2026-07-06T10:00:00Z', '2026-07-06T10:00:00Z');

-- #3 supersedes #2 (both halves present, the healthy case)
-- #2 supersedes #1 (both halves present)
-- #4 references #5, #5 contradicts #4  -> typed but NOT lineage
-- #6 carries ONLY superseded_by:7. #7 has NO supersedes entry -> mirror-only drift.
INSERT INTO memories_crossrefs (memory_id, related) VALUES
 (1, '[{"id":2,"score":0.91,"edge_type":"superseded_by"}]'),
 (2, '[{"id":1,"score":0.91,"edge_type":"supersedes"},{"id":3,"score":0.88,"edge_type":"superseded_by"}]'),
 (3, '[{"id":2,"score":0.88,"edge_type":"supersedes"}]'),
 (4, '[{"id":5,"score":0.77,"edge_type":"references"}]'),
 (5, '[{"id":4,"score":0.77,"edge_type":"contradicts"}]'),
 (6, '[{"id":7,"score":0.83,"edge_type":"superseded_by"}]'),
 (7, '[]'),
 (8, '[{"id":9,"score":0.93,"edge_type":"related_to"}]'),
 (9, '[{"id":8,"score":0.93,"edge_type":"related_to"}]');
