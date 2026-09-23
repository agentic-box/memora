/**
 * GET /api/capabilities - what this viewer may do.
 * The Pages viewer is read-only (docs/local-primary-implementation.md §6 F1,
 * slice L7): the shared index.html hides its edit controls unless a server
 * answers read_only: false, which only memora's own graph server does.
 */

export const onRequestGet: PagesFunction = async () =>
  Response.json({ read_only: true }, { headers: { "Cache-Control": "no-store" } });
