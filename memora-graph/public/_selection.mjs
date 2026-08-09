/**
 * Pure selection / detail-ownership helpers for force-graph.
 * Shipped module — tests MUST import this file (no mirrors).
 *
 * planSelectionReconcile: J1 post-commit selection decision.
 * detailResponseMayWrite / createDetailOwner: K1 async panel ownership.
 */

/**
 * After raw graph commits — reconcile CURRENT selection (not a start snapshot).
 * @param {number|null|undefined} curSelectedId
 * @param {boolean} panelOpenNow
 * @param {Array<{id:number, superseded?:boolean, authority_unknown?:boolean}>} nodes
 * @param {number[]|null|undefined} duplicateIds
 */
export function planSelectionReconcile(curSelectedId, panelOpenNow, nodes, duplicateIds) {
  if (curSelectedId == null) return { type: "none" };
  const graphNode = (nodes || []).find(n => n.id === curSelectedId);
  if (!graphNode) return { type: "clear" };
  return {
    type: "refresh",
    id: curSelectedId,
    openDetail: !!panelOpenNow,
    isDupe: Array.isArray(duplicateIds) && duplicateIds.includes(curSelectedId),
    superseded: !!graphNode.superseded,
    authority_unknown: !!graphNode.authority_unknown,
  };
}

/**
 * Pure ownership rule for detail responses (K1).
 * An older detail response must not mutate a panel that has moved on.
 */
export function detailResponseMayWrite({
  requestToken,
  currentToken,
  requestId,
  selectedId,
  panelOpen,
}) {
  return (
    requestToken === currentToken
    && panelOpen === true
    && selectedId === requestId
  );
}

/**
 * Mutable detail-session for the browser: monotonic token + AbortController.
 * invalidate() on open/close/deselect/clear; begin() at every openDetail start.
 */
export function createDetailOwner() {
  let token = 0;
  /** @type {AbortController|null} */
  let controller = null;

  function abortInFlight() {
    if (controller) {
      try { controller.abort(); } catch (_) { /* ignore */ }
      controller = null;
    }
  }

  return {
    /** @returns {{ token: number, signal: AbortSignal|undefined }} */
    begin() {
      token += 1;
      abortInFlight();
      controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      return {
        token,
        signal: controller ? controller.signal : undefined,
      };
    },
    /** Bump token and abort in-flight detail (close / deselect / clear). */
    invalidate() {
      token += 1;
      abortInFlight();
    },
    get token() {
      return token;
    },
    /**
     * @param {number} requestToken
     * @param {number} requestId
     * @param {number|null|undefined} selectedId
     * @param {boolean} panelOpen
     */
    mayWrite(requestToken, requestId, selectedId, panelOpen) {
      return detailResponseMayWrite({
        requestToken,
        currentToken: token,
        requestId,
        selectedId,
        panelOpen,
      });
    },
  };
}
