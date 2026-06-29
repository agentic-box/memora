"""HTML/CSS/JS templates for the knowledge graph visualization."""

from .issues import ISSUE_BADGE_CSS, ISSUE_FILTER_JS
from .todos import TODO_BADGE_CSS, TODO_FILTER_JS

# Base CSS styles shared by both static and dynamic graph
BASE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; display: flex; height: 100vh; }
#graph { flex: 1; height: 100%; }
div.vis-tooltip {
    background: linear-gradient(135deg, #1f2937 0%, #161b22 100%);
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 10px 14px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 13px;
    line-height: 1.5;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4), 0 2px 8px rgba(0,0,0,0.3);
    max-width: 320px;
    white-space: pre-wrap;
}
#panel { width: 450px; background: #161b22; border-left: 1px solid #30363d; padding: 20px; overflow-y: auto; display: none; position: relative; }
#panel.active { display: block; }
#panel h2 { color: #58a6ff; margin-bottom: 10px; font-size: 16px; }
#panel .tags { margin-bottom: 15px; }
#panel .tag { display: inline-block; background: #30363d; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin: 2px; cursor: pointer; }
#panel .tag:hover { background: #484f58; }
#panel .meta { color: #8b949e; font-size: 12px; margin-bottom: 15px; }
#panel .content { font-size: 13px; line-height: 1.6; background: #0d1117; padding: 15px; border-radius: 6px; max-height: calc(100vh - 200px); overflow-y: auto; }
#panel .content h1, #panel .content h2, #panel .content h3 { color: #58a6ff; margin: 16px 0 8px 0; }
#panel .content h1 { font-size: 1.4em; border-bottom: 1px solid #30363d; padding-bottom: 8px; }
#panel .content h2 { font-size: 1.2em; }
#panel .content h3 { font-size: 1.1em; }
#panel .content p { margin: 8px 0; }
#panel .content ul, #panel .content ol { margin: 8px 0 8px 20px; }
#panel .content li { margin: 4px 0; }
#panel .content code { background: #30363d; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 12px; }
#panel .content pre { background: #0d1117; border: 1px solid #30363d; padding: 12px; border-radius: 6px; overflow-x: auto; margin: 8px 0; }
#panel .content pre code { background: none; padding: 0; }
#panel .content a { color: #58a6ff; }
#panel .content table { border-collapse: collapse; margin: 8px 0; width: 100%; }
#panel .content th, #panel .content td { border: 1px solid #30363d; padding: 6px 10px; text-align: left; }
#panel .content th { background: #21262d; }
#panel .content blockquote { border-left: 3px solid #30363d; padding-left: 12px; margin: 8px 0; color: #8b949e; }
#panel .content .mermaid { background: #161b22; padding: 16px; border-radius: 6px; overflow-x: auto; margin: 8px 0; }
#panel .content .memory-images { margin-top: 16px; border-top: 1px solid #30363d; padding-top: 16px; }
#panel .content .memory-image { margin: 8px 0; }
#panel .content .memory-image img { max-width: 100%; border-radius: 6px; border: 1px solid #30363d; }
#panel .content .memory-image .caption { font-size: 11px; color: #8b949e; margin-top: 4px; text-align: center; }
#panel .content strong { color: #f0f6fc; }
#panel .close { position: absolute; top: 10px; right: 15px; cursor: pointer; font-size: 20px; color: #8b949e; }
#panel .close:hover { color: #fff; }
#resize-handle { width: 6px; background: #30363d; cursor: ew-resize; display: none; }
#resize-handle:hover, #resize-handle.dragging { background: #58a6ff; }
#resize-handle.active { display: block; }

/* Panel tabs */
#panel-tabs { display: flex; gap: 4px; background: #0d1117; padding: 6px; border-radius: 8px; margin-bottom: 16px; }
#panel-tabs .tab { padding: 8px 20px; cursor: pointer; color: #8b949e; border-radius: 6px; font-size: 13px; font-weight: 500; transition: all 0.15s ease; }
#panel-tabs .tab.active { color: #fff; background: linear-gradient(135deg, #238636 0%, #2ea043 100%); box-shadow: 0 2px 4px rgba(0,0,0,0.2); }
#panel-tabs .tab:not(.active):hover { color: #c9d1d9; background: #21262d; }
#tab-detail, #tab-timeline, #tab-history { display: none; }
#tab-detail.active, #tab-timeline.active, #tab-history.active { display: block; }
#timeline-list { max-height: calc(100vh - 120px); overflow-y: auto; }
#timeline-list .memory-item { padding: 10px; border-bottom: 1px solid #30363d; cursor: pointer; display: flex; flex-direction: column; }
#timeline-list .memory-item:hover { background: #21262d; }
#timeline-list .memory-item.selected { background: #30363d; }
#timeline-list .memory-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
#timeline-list .memory-title { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#timeline-list .memory-title .id { color: #58a6ff; font-weight: 500; font-size: 12px; }
#timeline-list .memory-title .headline { color: #c9d1d9; font-size: 12px; margin-left: 6px; }
#timeline-list .memory-actions { display: flex; gap: 6px; flex-shrink: 0; }
#timeline-list .memory-date { background: #21262d; border: 1px solid #30363d; color: #8b949e; padding: 2px 8px; border-radius: 4px; font-size: 10px; }
#timeline-list .details-btn { background: #21262d; border: 1px solid #30363d; color: #8b949e; padding: 2px 8px; border-radius: 4px; font-size: 10px; cursor: pointer; }
#timeline-list .details-btn:hover { background: #30363d; color: #c9d1d9; }
#timeline-list .memory-preview { color: #8b949e; font-size: 11px; line-height: 1.4; margin-top: 6px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
#timeline-list .favorite-star { cursor: pointer; font-size: 14px; color: #6e7681; margin-right: 4px; }
#timeline-list .favorite-star.active { color: #e3b341; }
#timeline-list .favorite-star:hover { color: #e3b341; }
#timeline-filter { display: flex; gap: 4px; padding: 8px 10px; border-bottom: 1px solid #30363d; }
#timeline-filter .filter-btn { background: none; border: 1px solid #30363d; color: #8b949e; padding: 3px 10px; border-radius: 12px; font-size: 11px; cursor: pointer; }
#timeline-filter .filter-btn:hover { background: #21262d; color: #c9d1d9; }
#timeline-filter .filter-btn.active { background: #30363d; color: #e3b341; border-color: #e3b341; }
#history-list { max-height: calc(100vh - 120px); overflow-y: auto; }
#history-list .action-item { padding: 8px 10px; border-bottom: 1px solid #30363d; display: flex; align-items: flex-start; gap: 8px; font-size: 12px; }
#history-list .action-icon { flex-shrink: 0; width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; }
#history-list .action-icon.create { background: #238636; color: #fff; }
#history-list .action-icon.update { background: #1f6feb; color: #fff; }
#history-list .action-icon.delete { background: #da3633; color: #fff; }
#history-list .action-icon.boost { background: #d29922; color: #fff; }
#history-list .action-icon.link { background: #8957e5; color: #fff; }
#history-list .action-icon.unlink { background: #6e7681; color: #fff; }
#history-list .action-icon.merge { background: #f78166; color: #fff; }
#history-list .action-body { flex: 1; min-width: 0; }
#history-list .action-summary { color: #c9d1d9; }
#history-list .action-summary .mem-link { color: #58a6ff; cursor: pointer; }
#history-list .action-summary .mem-link:hover { text-decoration: underline; }
#history-list .action-summary .mem-link.deleted { color: #6e7681; cursor: default; text-decoration: line-through; pointer-events: none; }
#history-list .action-count { color: #8b949e; font-size: 11px; margin-left: 4px; }
#history-list .action-time { color: #8b949e; font-size: 10px; margin-top: 2px; }

/* Timeline slider */
#timeline-container {
    position: absolute;
    bottom: 50px;
    left: 50%;
    transform: translateX(-50%);
    width: 280px;
    background: rgba(22,27,34,0.95);
    padding: 10px 14px;
    border-radius: 6px;
    border: 1px solid #30363d;
    z-index: 100;
}
#timeline-label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
    font-size: 11px;
    color: #8b949e;
}
#timeline-label .title { color: #58a6ff; font-weight: 500; }
#timeline-label .date-range { color: #c9d1d9; }
#timeline-slider {
    width: 100%;
    height: 6px;
    -webkit-appearance: none;
    appearance: none;
    background: linear-gradient(to right, #238636 0%, #58a6ff 100%, #30363d 100%);
    border-radius: 3px;
    outline: none;
    cursor: pointer;
}
#timeline-slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 18px;
    height: 18px;
    background: #58a6ff;
    border-radius: 50%;
    cursor: grab;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
    transition: transform 0.1s ease;
}
#timeline-slider::-webkit-slider-thumb:hover {
    transform: scale(1.15);
    background: #79b8ff;
}
#timeline-slider::-webkit-slider-thumb:active {
    cursor: grabbing;
    transform: scale(1.1);
}
#timeline-slider::-moz-range-thumb {
    width: 18px;
    height: 18px;
    background: #58a6ff;
    border-radius: 50%;
    cursor: grab;
    border: none;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
}
#timeline-dates {
    display: flex;
    justify-content: space-between;
    margin-top: 6px;
    font-size: 10px;
    color: #6e7681;
}
#legend {
    position: absolute;
    top: 10px;
    left: 10px;
    background: rgba(22,27,34,0.95);
    padding: 12px;
    border-radius: 6px;
    font-size: 12px;
    border-left: 3px solid #8b949e;
}
#legend > b { color: #c9d1d9; }
.legend-item { margin: 4px 0; display: flex; align-items: center; cursor: pointer; padding: 2px 4px; border-radius: 4px; }
.legend-item:hover { background: rgba(255,255,255,0.1); }
.legend-item.active { background: rgba(88,166,255,0.3); }
.legend-item.selected { color: #ffffff; }
.legend-color { width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; }
#legend .reset { margin-top: 8px; padding-top: 8px; border-top: 1px solid #30363d; color: #58a6ff; cursor: pointer; }
#legend-items { max-height: 0; overflow: hidden; transition: max-height 0.3s ease; }
#legend-items.expanded { max-height: 300px; }
.legend-toggle { cursor: pointer; color: #8b949e; font-size: 11px; margin-left: 4px; }
.legend-toggle:hover { color: #c9d1d9; }
#legend .reset:hover { text-decoration: underline; }
#duplicates-legend {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid #30363d;
}
#duplicates-legend .legend-color {
    font-size: 10px;
}
#sections {
    position: absolute;
    bottom: 50px;
    left: 10px;
    background: rgba(22,27,34,0.95);
    padding: 12px;
    border-radius: 0 6px 6px 0;
    font-size: 12px;
    max-height: 40vh;
    overflow-y: auto;
    white-space: nowrap;
    border-left: 3px solid #a855f7;
    border-top: 2px solid #a855f7;
}
#sections b { display: block; margin-bottom: 8px; color: #a855f7; }
.section-item { margin: 4px 0; cursor: pointer; padding: 3px 6px; border-radius: 4px; color: #a855f7; }
.section-item:hover { background: rgba(255,255,255,0.1); }
.section-item.active { background: rgba(168,85,247,0.3); }
.section-item.selected { color: #ffffff; }
.subsection-item { margin: 2px 0 2px 8px; cursor: pointer; padding: 2px 6px; border-radius: 4px; color: #8b949e; font-size: 11px; }
.subsection-item:hover { background: rgba(255,255,255,0.1); }
.subsection-item.active { background: rgba(88,166,255,0.3); color: #c9d1d9; }
.subsection-item.selected { color: #ffffff; }
#help { position: absolute; bottom: 10px; left: 10px; background: rgba(22,27,34,0.9); padding: 8px 12px; border-radius: 6px; font-size: 11px; color: #8b949e; }
#version { position: absolute; top: 10px; right: 470px; background: rgba(22,27,34,0.8); padding: 4px 10px; border-radius: 4px; font-size: 11px; color: #6e7681; z-index: 50; }
#node-tooltip { position: absolute; display: none; background: rgba(22,27,34,0.95); border: 1px solid #30363d; padding: 8px 12px; border-radius: 6px; pointer-events: none; z-index: 1000; max-width: 300px; }
#node-tooltip .tooltip-id { color: #58a6ff; font-size: 12px; font-weight: bold; }
#node-tooltip .tooltip-desc { color: #8b949e; font-size: 10px; margin-top: 4px; }
"""

# JavaScript for markdown/mermaid rendering
RENDER_JS = """
// Configure marked for GitHub-flavored markdown
marked.setOptions({ breaks: true, gfm: true });

// Initialize mermaid with dark theme
mermaid.initialize({
    startOnLoad: false,
    theme: 'dark',
    themeVariables: {
        primaryColor: '#58a6ff',
        primaryTextColor: '#c9d1d9',
        primaryBorderColor: '#30363d',
        lineColor: '#8b949e',
        secondaryColor: '#21262d',
        tertiaryColor: '#161b22'
    }
});

// Set up marked.js with custom renderer for mermaid
marked.use({
    renderer: {
        code: function(code, infostring, escaped) {
            var language = (infostring || '').trim().split(' ')[0];
            if (language === 'mermaid') {
                return '<div class="mermaid-pending">' + code + '</div>';
            }
            var esc = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            var langClass = language ? ' class="language-' + language + '"' : '';
            return '<pre><code' + langClass + '>' + esc + '</code></pre>';
        }
    }
});

function renderMarkdown(text) {
    return marked.parse(text);
}

async function renderMermaidBlocks() {
    var blocks = document.querySelectorAll('#panel-content .mermaid-pending');
    if (blocks.length === 0) return;

    for (var block of blocks) {
        block.className = 'mermaid';
        block.removeAttribute('data-processed');
    }

    // Wait for DOM to update before rendering
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

    try {
        var mermaidNodes = document.querySelectorAll('#panel-content .mermaid:not([data-processed])');
        await mermaid.run({ nodes: Array.from(mermaidNodes) });
    } catch (e) {
        console.error('Mermaid render error:', e);
    }
}

function renderImages(metadata) {
    var images = metadata && metadata.images;
    if (!images || images.length === 0) return '';
    var html = '<div class="memory-images">';
    for (var img of images) {
        html += '<div class="memory-image"><img src="' + img.src + '" alt="' + (img.caption || '') + '">';
        if (img.caption) html += '<div class="caption">' + img.caption + '</div>';
        html += '</div>';
    }
    return html + '</div>';
}

function renderIssueBadges(metadata) {
    if (!metadata || metadata.type !== 'issue') return '';
    var status = metadata.status || 'open';
    var closedReason = metadata.closed_reason || '';
    var severity = metadata.severity || 'unknown';
    var component = metadata.component || '';
    var commit = metadata.commit || '';

    // Build combined status key for color lookup
    var statusKey = status;
    var statusDisplay = status.toUpperCase();
    if (status === 'closed' && closedReason) {
        statusKey = 'closed:' + closedReason;
        statusDisplay = 'CLOSED (' + closedReason.toUpperCase().replace('_', ' ') + ')';
    }

    var statusColors = {open: '#ff7b72', 'closed:complete': '#7ee787', 'closed:not_planned': '#8b949e'};
    var severityColors = {critical: '#f85149', major: '#d29922', minor: '#8b949e'};

    var html = '<div class="issue-badges">';
    html += '<span class="issue-badge" style="background:' + (statusColors[statusKey] || '#8b949e') + '">' + statusDisplay + '</span>';
    html += '<span class="issue-badge" style="background:' + (severityColors[severity] || '#8b949e') + '">' + severity + '</span>';
    if (component) html += '<span class="issue-badge component">' + component + '</span>';
    if (commit) html += '<span class="issue-badge commit">#' + commit.slice(0,7) + '</span>';
    html += '</div>';
    return html;
}

function renderTodoBadges(metadata) {
    if (!metadata || metadata.type !== 'todo') return '';
    var status = metadata.status || 'open';
    var closedReason = metadata.closed_reason || '';
    var priority = metadata.priority || 'medium';
    var category = metadata.category || '';

    // Build combined status key for color lookup
    var statusKey = status;
    var statusDisplay = status.toUpperCase();
    if (status === 'closed' && closedReason) {
        statusKey = 'closed:' + closedReason;
        statusDisplay = 'CLOSED (' + closedReason.toUpperCase().replace('_', ' ') + ')';
    }

    var statusColors = {open: '#58a6ff', 'closed:complete': '#7ee787', 'closed:not_planned': '#8b949e'};
    var priorityColors = {high: '#f85149', medium: '#d29922', low: '#8b949e'};

    var html = '<div class="todo-badges">';
    html += '<span class="todo-badge" style="background:' + (statusColors[statusKey] || '#8b949e') + '">' + statusDisplay + '</span>';
    html += '<span class="todo-badge" style="background:' + (priorityColors[priority] || '#8b949e') + '">' + priority + '</span>';
    if (category) html += '<span class="todo-badge category">' + category + '</span>';
    html += '</div>';
    return html;
}
"""

# JavaScript for filtering
FILTER_JS = """
function toggleSection(el) {
    var parent = el.parentElement;
    parent.classList.toggle('collapsed');
    el.textContent = parent.classList.contains('collapsed') ? '[+]' : '[-]';
}

function filterByDuplicates() {
    document.querySelectorAll('.legend-item, .section-item, .subsection-item, .cluster-item').forEach(el => el.classList.remove('active'));
    var el = document.querySelector('#duplicates-legend .legend-item');
    if (el) el.classList.add('active');
    currentFilter = 'duplicates';
    var nodeIds = typeof graphData !== 'undefined' ? graphData.duplicateIds : (typeof duplicateIds !== 'undefined' ? duplicateIds : []);
    applyFilter(nodeIds);
}

function filterByTag(tag) {
    document.querySelectorAll('.legend-item, .section-item, .subsection-item, .cluster-item').forEach(el => el.classList.remove('active'));
    var el = document.querySelector('.legend-item[data-tag="' + tag + '"]');
    if (el) el.classList.add('active');
    currentFilter = tag;
    var nodeIds = (typeof graphData !== 'undefined' ? graphData.tagToNodes : tagToNodes)[tag] || [];
    applyFilter(nodeIds);
}

function filterBySection(section) {
    document.querySelectorAll('.legend-item, .section-item, .subsection-item, .cluster-item').forEach(el => el.classList.remove('active'));
    var el = document.querySelector('.section-item[data-section="' + section + '"]');
    if (el) el.classList.add('active');
    currentFilter = section;
    var nodeIds = (typeof graphData !== 'undefined' ? graphData.sectionToNodes : sectionToNodes)[section] || [];
    applyFilter(nodeIds);
}

function filterBySubsection(subsection) {
    document.querySelectorAll('.legend-item, .section-item, .subsection-item, .cluster-item').forEach(el => el.classList.remove('active'));
    var el = document.querySelector('.subsection-item[data-subsection="' + subsection + '"]');
    if (el) el.classList.add('active');
    currentFilter = subsection;
    var nodeIds = (typeof graphData !== 'undefined' ? graphData.subsectionToNodes : subsectionToNodes)[subsection] || [];
    applyFilter(nodeIds);
}

function applyFilter(nodeIds) {
    var nodeSet = new Set(nodeIds);
    var sourceNodes = typeof graphData !== 'undefined' ? graphData.nodes : allNodes;
    var sourceEdges = typeof graphData !== 'undefined' ? graphData.edges : allEdges;
    nodes.clear();
    edges.clear();
    var filteredNodes = sourceNodes.filter(n => nodeSet.has(n.id));
    var filteredEdges = sourceEdges.filter(e => nodeSet.has(e.from) && nodeSet.has(e.to));
    nodes.add(filteredNodes);
    edges.add(filteredEdges);
    network.fit({ animation: true });
}

function resetFilter() {
    document.querySelectorAll('.legend-item, .section-item, .subsection-item, .cluster-item').forEach(el => el.classList.remove('active'));
    currentFilter = null;
    exitFocusMode();
    var sourceNodes = typeof graphData !== 'undefined' ? graphData.nodes : allNodes;
    var sourceEdges = typeof graphData !== 'undefined' ? graphData.edges : allEdges;
    nodes.clear();
    edges.clear();
    nodes.add(sourceNodes);
    edges.add(sourceEdges);
    network.fit({ animation: true });
}

var focusedNodeId = null;

function getConnectedNodes(nodeId, hops) {
    var connected = new Set([nodeId]);
    var sourceEdges = typeof graphData !== 'undefined' ? graphData.edges : allEdges;
    for (var h = 0; h < hops; h++) {
        var toAdd = [];
        sourceEdges.forEach(function(e) {
            if (connected.has(e.from)) toAdd.push(e.to);
            if (connected.has(e.to)) toAdd.push(e.from);
        });
        toAdd.forEach(function(id) { connected.add(id); });
    }
    return connected;
}

function focusOnNode(nodeId) {
    // Reset timeline when focusing on a node to avoid conflicting states
    if (timelineActive) {
        resetTimeline();
    }

    focusedNodeId = nodeId;
    var hop1 = getConnectedNodes(nodeId, 1);  // Direct connections
    var hop2 = getConnectedNodes(nodeId, 2);  // Includes hop1 + indirect
    var sourceNodes = typeof graphData !== 'undefined' ? graphData.nodes : allNodes;

    // Only update currently visible nodes (respect filters)
    var visibleNodeIds = new Set(nodes.getIds());
    var visibleEdgeIds = new Set(edges.getIds());

    // Update nodes with opacity - use update() to preserve positions
    var nodeUpdates = sourceNodes.filter(function(n) {
        return visibleNodeIds.has(n.id);
    }).map(function(n) {
        if (n.id === nodeId) {
            return { id: n.id, borderWidth: 4, color: { background: n.color.background || n.color, border: '#58a6ff' }, opacity: 1 };
        } else if (hop1.has(n.id)) {
            // Direct connections - full visibility
            return { id: n.id, borderWidth: n.borderWidth || 2, color: n.color, opacity: 1 };
        } else if (hop2.has(n.id)) {
            // Indirect connections - mostly faded
            return { id: n.id, borderWidth: n.borderWidth || 2, color: n.color, opacity: 0.35 };
        } else {
            // Unconnected (hop 3+) - nearly invisible
            return { id: n.id, borderWidth: n.borderWidth || 2, color: n.color, opacity: 0.08 };
        }
    });

    // Update edges with visual hierarchy - only visible ones
    var sourceEdges = typeof graphData !== 'undefined' ? graphData.edges : allEdges;
    var edgeUpdates = sourceEdges.filter(function(e) {
        return visibleEdgeIds.has(e.id);
    }).map(function(e) {
        // Hop 1: edges directly connected to focused node - thick cyan
        if (e.from === nodeId || e.to === nodeId) {
            return { id: e.id, width: 4, color: '#4CC9F0' };
        }
        // Hop 2: edges between connected nodes - thin faded grey
        else if (hop2.has(e.from) && hop2.has(e.to)) {
            return { id: e.id, width: 1, color: 'rgba(139,148,158,0.35)' };
        }
        // Unconnected (hop 3+): nearly invisible
        else {
            return { id: e.id, width: 1, color: 'rgba(48,54,61,0.05)' };
        }
    });

    nodes.update(nodeUpdates);
    edges.update(edgeUpdates);
}

function exitFocusMode() {
    if (!focusedNodeId) return;
    focusedNodeId = null;
    var sourceNodes = typeof graphData !== 'undefined' ? graphData.nodes : allNodes;
    var sourceEdges = typeof graphData !== 'undefined' ? graphData.edges : allEdges;

    // Only update currently visible nodes (respect filters)
    var visibleNodeIds = new Set(nodes.getIds());
    var visibleEdgeIds = new Set(edges.getIds());

    // Restore original node styles - use update() to preserve positions
    var nodeUpdates = sourceNodes.filter(function(n) {
        return visibleNodeIds.has(n.id);
    }).map(function(n) {
        return { id: n.id, borderWidth: n.borderWidth || 2, color: n.color, opacity: 1 };
    });
    var edgeUpdates = sourceEdges.filter(function(e) {
        return visibleEdgeIds.has(e.id);
    }).map(function(e) {
        return { id: e.id, width: 1, color: e.color || 'rgba(48,54,61,0.6)' };
    });

    nodes.update(nodeUpdates);
    edges.update(edgeUpdates);
}
"""

# JavaScript for resize handle
RESIZE_JS = """
var resizeHandle = document.getElementById('resize-handle');
var panel = document.getElementById('panel');
var isResizing = false;

resizeHandle.addEventListener('mousedown', function(e) {
    isResizing = true;
    resizeHandle.classList.add('dragging');
    document.body.style.cursor = 'ew-resize';
    e.preventDefault();
});

document.addEventListener('mousemove', function(e) {
    if (!isResizing) return;
    var newWidth = window.innerWidth - e.clientX;
    if (newWidth >= 200 && newWidth <= 800) {
        panel.style.width = newWidth + 'px';
        updateTimelinePosition();
    }
});

document.addEventListener('mouseup', function() {
    isResizing = false;
    resizeHandle.classList.remove('dragging');
    document.body.style.cursor = '';
});
"""

# JavaScript for timeline slider
TIMELINE_JS = """
var timelineData = null;
var timelineActive = false;

function initTimeline(nodeTimestamps, minDate, maxDate) {
    if (!nodeTimestamps || Object.keys(nodeTimestamps).length === 0) return;

    timelineData = {
        timestamps: nodeTimestamps,
        minTime: new Date(minDate).getTime(),
        maxTime: new Date(maxDate).getTime(),
        sortedNodes: Object.entries(nodeTimestamps)
            .map(([id, ts]) => ({ id: parseInt(id), time: new Date(ts).getTime() }))
            .sort((a, b) => a.time - b.time)
    };

    // Set date labels
    document.getElementById('timeline-min-date').textContent = formatDate(minDate);
    document.getElementById('timeline-max-date').textContent = formatDate(maxDate);

    // Initialize slider to 100% (show all)
    var slider = document.getElementById('timeline-slider');
    slider.value = 100;
    updateTimelineProgress(100);

    // Show container
    document.getElementById('timeline-container').style.display = 'block';
}

function formatDate(dateStr) {
    var d = new Date(dateStr);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
}

function updateTimelineProgress(percent) {
    var slider = document.getElementById('timeline-slider');
    // Update slider track: colored portion (green->blue gradient) + gray remainder
    slider.style.background = 'linear-gradient(to right, #238636 0%, #58a6ff ' + percent + '%, #30363d ' + percent + '%)';
}

function onTimelineChange(value) {
    if (!timelineData) return;

    // Exit focus mode when using timeline to avoid conflicting states
    exitFocusMode();

    timelineActive = true;
    var percent = parseInt(value);
    updateTimelineProgress(percent);

    // Calculate cutoff time based on slider position
    var timeRange = timelineData.maxTime - timelineData.minTime;
    var cutoffTime = timelineData.minTime + (timeRange * percent / 100);

    // Update date display
    var cutoffDate = new Date(cutoffTime);
    document.getElementById('timeline-current').textContent =
        'Showing: ' + formatDate(cutoffDate.toISOString());

    // Get node IDs up to cutoff
    // Nodes without timestamps are always visible (they predate timestamp tracking)
    var visibleIds = new Set();
    var sourceNodes = typeof graphData !== 'undefined' ? graphData.nodes : allNodes;
    for (var n of sourceNodes) {
        if (!timelineData.timestamps[n.id]) {
            // No timestamp = always visible
            visibleIds.add(n.id);
        }
    }
    for (var node of timelineData.sortedNodes) {
        if (node.time <= cutoffTime) {
            visibleIds.add(node.id);
        }
    }

    // Update node visibility with fade effect
    var sourceNodes = typeof graphData !== 'undefined' ? graphData.nodes : allNodes;
    var nodeUpdates = sourceNodes.map(function(n) {
        if (visibleIds.has(n.id)) {
            // Visible - full opacity, highlight newer ones
            var nodeTime = timelineData.timestamps[n.id] ? new Date(timelineData.timestamps[n.id]).getTime() : 0;
            var recency = (nodeTime - timelineData.minTime) / timeRange;
            // Highlight recently revealed nodes with a glow
            var isRecent = Math.abs(nodeTime - cutoffTime) < (timeRange * 0.05);
            return {
                id: n.id,
                opacity: 1,
                borderWidth: isRecent ? 4 : (n.borderWidth || 2),
                color: isRecent ? { background: n.color.background || n.color, border: '#58a6ff' } : n.color
            };
        } else {
            // Hidden - very faded
            return { id: n.id, opacity: 0.08 };
        }
    });

    // Update edges - only show edges between visible nodes
    var sourceEdges = typeof graphData !== 'undefined' ? graphData.edges : allEdges;
    var edgeUpdates = sourceEdges.map(function(e) {
        if (visibleIds.has(e.from) && visibleIds.has(e.to)) {
            return { id: e.id, hidden: false, color: e.color || 'rgba(48,54,61,0.6)' };
        } else {
            return { id: e.id, hidden: true };
        }
    });

    nodes.update(nodeUpdates);
    edges.update(edgeUpdates);
}

function updateTimelinePosition() {
    var timeline = document.getElementById('timeline-container');
    if (!timeline) return;
    var panel = document.getElementById('panel');
    var panelOpen = panel && panel.classList.contains('active');
    if (panelOpen) {
        var panelWidth = panel.offsetWidth || 450;
        timeline.style.left = 'calc(50% - ' + (panelWidth / 2) + 'px)';
    } else {
        timeline.style.left = '50%';
    }
}

function resetTimeline() {
    if (!timelineData) return;

    timelineActive = false;
    var slider = document.getElementById('timeline-slider');
    slider.value = 100;
    updateTimelineProgress(100);
    document.getElementById('timeline-current').textContent = 'Drag to filter by time';

    // Restore all nodes
    var sourceNodes = typeof graphData !== 'undefined' ? graphData.nodes : allNodes;
    var sourceEdges = typeof graphData !== 'undefined' ? graphData.edges : allEdges;

    var nodeUpdates = sourceNodes.map(function(n) {
        return { id: n.id, opacity: 1, borderWidth: n.borderWidth || 2, color: n.color };
    });
    var edgeUpdates = sourceEdges.map(function(e) {
        return { id: e.id, hidden: false, color: e.color || 'rgba(48,54,61,0.6)' };
    });

    nodes.update(nodeUpdates);
    edges.update(edgeUpdates);
}
"""

# JavaScript for custom tooltip
TOOLTIP_JS = """
function showNodeTooltip(nodeId, pointer) {
    var node = nodes.get(nodeId);
    if (!node || !node.title) return;
    var parts = node.title.split('\\n');
    var idLine = parts[0] || '';
    var descLine = parts.slice(1).join(' ') || '';
    var tooltip = document.getElementById('node-tooltip');
    tooltip.innerHTML = '<div class="tooltip-id">' + idLine + '</div>' +
                        (descLine ? '<div class="tooltip-desc">' + descLine + '</div>' : '');
    tooltip.style.left = (pointer.DOM.x + 15) + 'px';
    tooltip.style.top = (pointer.DOM.y + 15) + 'px';
    tooltip.style.display = 'block';
}

function hideNodeTooltip() {
    document.getElementById('node-tooltip').style.display = 'none';
}
"""

# JavaScript for panel display
PANEL_JS = """
var currentPanelMemoryId = null;
var currentTab = 'detail';

function switchTab(tabName) {
    currentTab = tabName;
    document.querySelectorAll('#panel-tabs .tab').forEach(function(t) { t.classList.remove('active'); });
    document.querySelector('#panel-tabs .tab[onclick*="' + tabName + '"]').classList.add('active');
    document.getElementById('tab-detail').classList.toggle('active', tabName === 'detail');
    document.getElementById('tab-timeline').classList.toggle('active', tabName === 'timeline');
    document.getElementById('tab-history').classList.toggle('active', tabName === 'history');
    if (tabName === 'timeline') {
        populateTimelineList();
    } else if (tabName === 'history') {
        populateHistoryList();
    } else if (tabName === 'detail' && currentPanelMemoryId) {
        loadMemoryToPanel(currentPanelMemoryId);
    }
}

function loadMemoryToPanel(memId) {
    memId = parseInt(memId, 10);
    if (typeof memoriesData !== 'undefined' && memoriesData[memId]) {
        showPanel(memoriesData[memId]);
    } else if (typeof memoryCache !== 'undefined' && memoryCache[memId]) {
        showPanel(memoryCache[memId]);
    } else {
        fetch('/api/memories/' + memId)
            .then(function(r) { return r.json(); })
            .then(function(mem) {
                if (!mem.error) {
                    if (typeof memoryCache !== 'undefined') memoryCache[memId] = mem;
                    showPanel(mem);
                }
            });
    }
}

function populateTimelineList() {
    // Check if memoriesData exists (static template) or fetch from API (SPA)
    if (typeof memoriesData !== 'undefined') {
        renderTimelineList(Object.values(memoriesData));
    } else {
        // SPA mode - fetch from API
        document.getElementById('timeline-list').innerHTML = '<div style="padding:20px;color:#8b949e;">Loading...</div>';
        fetch('/api/memories')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.memories) {
                    renderTimelineList(data.memories);
                }
            })
            .catch(function(e) {
                document.getElementById('timeline-list').innerHTML = '<div style="padding:20px;color:#f85149;">Error loading memories</div>';
            });
    }
}

var timelineFilter = 'all';
var cachedTimelineMemories = null;

function filterTimeline(mode) {
    timelineFilter = mode;
    document.querySelectorAll('#timeline-filter .filter-btn').forEach(function(btn) {
        btn.classList.toggle('active', btn.textContent.indexOf(mode === 'favorites' ? 'Favorites' : 'Show All') !== -1);
    });
    if (cachedTimelineMemories) renderTimelineList(cachedTimelineMemories);
}

function renderTimelineList(memories) {
    // Filter out section placeholders
    memories = memories.filter(function(mem) {
        return !(mem.metadata && mem.metadata.type === 'section');
    });
    cachedTimelineMemories = memories;
    if (timelineFilter === 'favorites') {
        memories = memories.filter(function(mem) { return mem.metadata && mem.metadata.favorite; });
    }
    memories.sort(function(a, b) {
        return new Date(b.created) - new Date(a.created);
    });

    var html = memories.map(function(mem) {
        var headline = getMemoryHeadline(mem.content);
        var preview = getMemoryPreview(mem.content);
        var selectedClass = (currentPanelMemoryId === mem.id) ? ' selected' : '';
        var isFav = mem.metadata && mem.metadata.favorite;
        var starClass = 'favorite-star' + (isFav ? ' active' : '');
        var starIcon = isFav ? '\\u2605' : '\\u2606';
        return '<div class="memory-item' + selectedClass + '" data-id="' + mem.id + '" onclick="highlightMemoryInGraph(' + mem.id + ')">' +
            '<div class="memory-header">' +
                '<div class="memory-title"><span class="' + starClass + '" onclick="toggleFavorite(' + mem.id + ', this); event.stopPropagation();">' + starIcon + '</span><span class="id">#' + mem.id + '</span><span class="headline">' + escapeHtmlText(headline) + '</span></div>' +
                '<div class="memory-actions">' +
                    '<span class="memory-date">' + mem.created + '</span>' +
                    '<button class="details-btn" onclick="showMemoryDetails(' + mem.id + '); event.stopPropagation();">Details</button>' +
                '</div>' +
            '</div>' +
            '<div class="memory-preview">' + escapeHtmlText(preview) + '</div>' +
        '</div>';
    }).join('');
    var emptyMsg = timelineFilter === 'favorites' ? 'No favorites yet — click \\u2606 to star a memory' : 'No memories';
    document.getElementById('timeline-list').innerHTML = html || '<div style="padding:20px;color:#8b949e;">' + emptyMsg + '</div>';

    // Scroll selected item into view
    var selected = document.querySelector('#timeline-list .memory-item.selected');
    if (selected) selected.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

function toggleFavorite(memId, el) {
    var isActive = el.classList.contains('active');
    var newState = !isActive;
    el.classList.toggle('active', newState);
    el.textContent = newState ? '\\u2605' : '\\u2606';
    if (cachedTimelineMemories) {
        cachedTimelineMemories.forEach(function(mem) {
            if (mem.id === memId) {
                if (!mem.metadata) mem.metadata = {};
                if (newState) { mem.metadata.favorite = true; } else { delete mem.metadata.favorite; }
            }
        });
    }
    fetch('/api/memories/' + memId + '/favorite', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ favorite: newState })
    });
}

function populateHistoryList() {
    fetch('/api/actions?limit=200')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.actions) renderHistoryList(data.actions);
        })
        .catch(function() {
            document.getElementById('history-list').innerHTML = '<div style="padding:20px;color:#8b949e;">History is only available on the live server.</div>';
        });
}

function renderHistoryList(actions) {
    var iconMap = { create: '+', update: '~', delete: 'x', boost: '^', link: '&rarr;', unlink: '&times;', merge: '&oplus;' };
    var existingIds = graphData ? new Set(graphData.nodes.map(function(n) { return n.id; })) : new Set();
    // Group consecutive actions with same action type
    var grouped = [];
    actions.forEach(function(a) {
        var last = grouped[grouped.length - 1];
        if (last && last.action === a.action) {
            last.items.push(a);
            last.last_ts = a.timestamp;
        } else {
            grouped.push({ action: a.action, items: [a], timestamp: a.timestamp, last_ts: a.timestamp });
        }
    });
    var html = grouped.map(function(g) {
        var icon = iconMap[g.action] || '?';
        var cls = g.action;
        if (g.items.length === 1) {
            var a = g.items[0];
            var summary = escapeHtmlText(a.summary).replace(/#(\\d+)/g, function(match, id) {
                if (existingIds.has(parseInt(id, 10))) {
                    return '<span class="mem-link" onclick="showMemoryDetails(' + id + '); event.stopPropagation();">#' + id + '</span>';
                }
                return '<span class="mem-link deleted">#' + id + '</span>';
            });
            return '<div class="action-item">' +
                '<div class="action-icon ' + cls + '">' + icon + '</div>' +
                '<div class="action-body">' +
                    '<div class="action-summary">' + summary + '</div>' +
                    '<div class="action-time">' + a.timestamp + '</div>' +
                '</div>' +
            '</div>';
        }
        // Collapsed group
        var ids = g.items.map(function(a) { return a.memory_id; });
        var idLinks = ids.map(function(mid) {
            if (existingIds.has(mid)) {
                return '<span class="mem-link" onclick="showMemoryDetails(' + mid + '); event.stopPropagation();">#' + mid + '</span>';
            }
            return '<span class="mem-link deleted">#' + mid + '</span>';
        }).join(', ');
        var label = g.action.charAt(0).toUpperCase() + g.action.slice(1) + 'd';
        return '<div class="action-item">' +
            '<div class="action-icon ' + cls + '">' + icon + '</div>' +
            '<div class="action-body">' +
                '<div class="action-summary">' + label + ' ' + g.items.length + ' memories: ' + idLinks + '</div>' +
                '<div class="action-time">' + g.timestamp + '</div>' +
            '</div>' +
        '</div>';
    }).join('');
    document.getElementById('history-list').innerHTML = html || '<div style="padding:20px;color:#8b949e;">No actions recorded yet.</div>';
}

function highlightMemoryInGraph(memId) {
    memId = parseInt(memId, 10);
    // Focus on the node in the graph (highlights connections)
    if (typeof focusOnNode !== 'undefined') {
        focusOnNode(memId);
    }
    // Store as current panel memory so switching to Details tab shows correct memory
    currentPanelMemoryId = memId;
    // Update selected state in timeline
    document.querySelectorAll('#timeline-list .memory-item').forEach(function(el) {
        el.classList.toggle('selected', parseInt(el.dataset.id, 10) === memId);
    });
    // Fetch fresh data and highlight section (no cache for data integrity)
    fetch('/api/memories/' + memId)
        .then(function(r) { return r.json(); })
        .then(function(mem) {
            if (!mem.error) {
                highlightMemorySection(mem);
            }
        });
}

function highlightMemorySection(mem) {
    // Clear previous selection
    document.querySelectorAll('.subsection-item.selected, .section-item.selected, .legend-item.selected').forEach(function(el) { el.classList.remove('selected'); });
    if (!mem.metadata) return;
    // Handle issues
    if (mem.metadata.type === 'issue' && mem.metadata.status) {
        var statusKey = mem.metadata.status;
        if (statusKey === 'closed' && mem.metadata.closed_reason) {
            statusKey = 'closed:' + mem.metadata.closed_reason;
        }
        var issueEl = document.querySelector('.legend-item.issue-status[data-status="' + statusKey + '"]');
        if (issueEl) issueEl.classList.add('selected');
    }
    // Handle TODOs
    else if (mem.metadata.type === 'todo' && mem.metadata.status) {
        var statusKey = mem.metadata.status;
        if (statusKey === 'closed' && mem.metadata.closed_reason) {
            statusKey = 'closed:' + mem.metadata.closed_reason;
        }
        var todoEl = document.querySelector('.legend-item.todo-status[data-todo-status="' + statusKey + '"]');
        if (todoEl) todoEl.classList.add('selected');
    }
    // Handle regular memories with sections
    else {
        var section, subsection;
        var hierarchy = mem.metadata.hierarchy;
        if (hierarchy && hierarchy.path && hierarchy.path.length >= 1) {
            section = hierarchy.path[0];
            subsection = hierarchy.path.slice(1).join('/');
        } else {
            section = mem.metadata.section;
            subsection = mem.metadata.subsection;
        }
        if (section) {
            var sectionEl = document.querySelector('.section-item[data-section="' + section + '"]');
            if (sectionEl) sectionEl.classList.add('selected');
            if (subsection) {
                var path = section + '/' + subsection;
                var el = document.querySelector('.subsection-item[data-subsection="' + path + '"]');
                if (el) el.classList.add('selected');
            }
        }
    }
}

function showMemoryDetails(memId) {
    memId = parseInt(memId, 10);
    // Switch to detail tab and show panel
    switchTab('detail');
    // Get memory data
    if (typeof memoriesData !== 'undefined' && memoriesData[memId]) {
        showPanel(memoriesData[memId]);
    } else if (typeof memoryCache !== 'undefined' && memoryCache[memId]) {
        showPanel(memoryCache[memId]);
    } else {
        fetch('/api/memories/' + memId)
            .then(function(r) { return r.json(); })
            .then(function(mem) {
                if (!mem.error) {
                    if (typeof memoryCache !== 'undefined') memoryCache[memId] = mem;
                    showPanel(mem);
                }
            });
    }
}

function getMemoryHeadline(content) {
    var lines = content.split('\\n').filter(function(l) { return l.trim(); });
    var first = lines[0] || '';
    return first.replace(/^#+\\s*/, '').substring(0, 80);
}

function getMemoryPreview(content) {
    var lines = content.split('\\n').filter(function(l) { return l.trim() && !l.match(/^#+/); });
    return lines.slice(0, 2).join(' ').substring(0, 150);
}

function escapeHtmlText(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function closePanel() {
    document.getElementById('panel').classList.remove('active');
    document.getElementById('resize-handle').classList.remove('active');
    currentPanelMemoryId = null;
    document.querySelectorAll('.subsection-item.selected, .section-item.selected').forEach(el => el.classList.remove('selected'));
    updateTimelinePosition();
}

function showPanel(mem) {
    currentPanelMemoryId = mem.id;

    // Ensure detail tab is active when showing a specific memory
    if (currentTab !== 'detail') {
        document.querySelectorAll('#panel-tabs .tab').forEach(function(t) { t.classList.remove('active'); });
        document.querySelector('#panel-tabs .tab[onclick*="detail"]').classList.add('active');
        document.getElementById('tab-detail').classList.add('active');
        document.getElementById('tab-timeline').classList.remove('active');
        document.getElementById('tab-history').classList.remove('active');
        currentTab = 'detail';
    }

    document.getElementById('panel-title').textContent = 'Memory #' + mem.id;

    // Show issue or TODO badges if applicable
    var badgesHtml = renderIssueBadges(mem.metadata) + renderTodoBadges(mem.metadata);
    var metaHtml = badgesHtml + 'Created: ' + mem.created;
    if (mem.updated) {
        metaHtml += '<br>Updated: ' + mem.updated;
    }
    document.getElementById('panel-meta').innerHTML = metaHtml;

    document.getElementById('panel-tags').innerHTML = mem.tags.map(function(t) {
        return '<span class="tag" onclick="filterByTag(\\'' + t + '\\'); event.stopPropagation();">' + t + '</span>';
    }).join('');

    document.getElementById('panel-content').innerHTML = renderMarkdown(mem.content);
    renderMermaidBlocks();
    document.getElementById('panel-content').innerHTML += renderImages(mem.metadata);
    document.getElementById('panel').classList.add('active');
    document.getElementById('resize-handle').classList.add('active');

    // Highlight the memory's subsection/status in the left pane
    highlightMemorySection(mem);
    updateTimelinePosition();
}
"""


CLUSTER_CSS = """
"""

CLUSTER_JS = """
function computeConvexHull(points) {
    if (points.length <= 1) return points;
    points.sort(function(a, b) { return a.x - b.x || a.y - b.y; });
    if (points.length <= 2) return points.slice();

    var lower = [];
    for (var i = 0; i < points.length; i++) {
        while (lower.length >= 2 && cross(lower[lower.length-2], lower[lower.length-1], points[i]) <= 0)
            lower.pop();
        lower.push(points[i]);
    }
    var upper = [];
    for (var i = points.length - 1; i >= 0; i--) {
        while (upper.length >= 2 && cross(upper[upper.length-2], upper[upper.length-1], points[i]) <= 0)
            upper.pop();
        upper.push(points[i]);
    }
    upper.pop();
    lower.pop();
    return lower.concat(upper);
}

function cross(O, A, B) {
    return (A.x - O.x) * (B.y - O.y) - (A.y - O.y) * (B.x - O.x);
}

function expandHull(hull, padding) {
    if (hull.length < 3) return hull;
    var cx = 0, cy = 0;
    for (var i = 0; i < hull.length; i++) { cx += hull[i].x; cy += hull[i].y; }
    cx /= hull.length; cy /= hull.length;
    var expanded = [];
    for (var i = 0; i < hull.length; i++) {
        var dx = hull[i].x - cx, dy = hull[i].y - cy;
        var dist = Math.sqrt(dx*dx + dy*dy);
        if (dist === 0) { expanded.push({x: hull[i].x, y: hull[i].y}); continue; }
        expanded.push({x: hull[i].x + dx/dist * padding, y: hull[i].y + dy/dist * padding});
    }
    return expanded;
}

function drawSmoothClosed(ctx, hull) {
    // Draw a smooth closed curve through hull points using Catmull-Rom splines
    var n = hull.length;
    if (n < 3) return;
    ctx.beginPath();
    for (var i = 0; i < n; i++) {
        var p0 = hull[(i - 1 + n) % n];
        var p1 = hull[i];
        var p2 = hull[(i + 1) % n];
        var p3 = hull[(i + 2) % n];
        // Catmull-Rom to cubic bezier conversion (alpha=0.5 centripetal)
        var tension = 6;
        var cp1x = p1.x + (p2.x - p0.x) / tension;
        var cp1y = p1.y + (p2.y - p0.y) / tension;
        var cp2x = p2.x - (p3.x - p1.x) / tension;
        var cp2y = p2.y - (p3.y - p1.y) / tension;
        if (i === 0) ctx.moveTo(p1.x, p1.y);
        ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }
    ctx.closePath();
}

function drawClusterHulls(ctx) {
    var clusterToNodes = (typeof graphData !== 'undefined' && graphData) ? graphData.clusterToNodes : (typeof window.clusterToNodes !== 'undefined' ? window.clusterToNodes : {});
    var clusterColors = (typeof graphData !== 'undefined' && graphData) ? graphData.clusterColors : (typeof window.clusterColors !== 'undefined' ? window.clusterColors : {});
    if (!clusterToNodes || !clusterColors) return;

    for (var cid in clusterToNodes) {
        var memberIds = clusterToNodes[cid];
        if (!memberIds || memberIds.length < 3) continue;

        var positions = network.getPositions(memberIds);
        var points = [];
        for (var id of memberIds) {
            if (positions[id]) points.push({x: positions[id].x, y: positions[id].y});
        }
        if (points.length < 3) continue;

        var hull = computeConvexHull(points);
        if (hull.length < 3) continue;
        hull = expandHull(hull, 45);

        var color = clusterColors[cid] || '#8b949e';
        ctx.save();

        // Draw smooth curved boundary
        drawSmoothClosed(ctx, hull);

        // Fill with low alpha
        ctx.fillStyle = color + '14';
        ctx.fill();

        // Stroke with smooth line
        ctx.strokeStyle = color + '55';
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.restore();
    }
}

function seedClusterPositions(nodeArray) {
    // Assign initial (x,y) to nodes grouped by cluster so physics starts clustered
    var clusterToNodes = (typeof graphData !== 'undefined' && graphData) ? graphData.clusterToNodes : (typeof window.clusterToNodes !== 'undefined' ? window.clusterToNodes : {});
    if (!clusterToNodes || Object.keys(clusterToNodes).length === 0) return;

    var clusterIds = Object.keys(clusterToNodes);
    var nClusters = clusterIds.length;
    // Lay out cluster centers in a circle
    var radius = 250 + nClusters * 40;
    var centers = {};
    for (var i = 0; i < nClusters; i++) {
        var angle = (2 * Math.PI * i) / nClusters;
        centers[clusterIds[i]] = { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
    }

    // Build node -> cluster lookup
    var nodeCluster = {};
    for (var cid in clusterToNodes) {
        var members = clusterToNodes[cid];
        for (var j = 0; j < members.length; j++) {
            nodeCluster[members[j]] = cid;
        }
    }

    // Set initial positions: scatter around cluster center
    var spread = 80 + Math.sqrt(nodeArray.length) * 8;
    for (var k = 0; k < nodeArray.length; k++) {
        var node = nodeArray[k];
        var cid = nodeCluster[node.id];
        if (cid && centers[cid]) {
            node.x = centers[cid].x + (Math.random() - 0.5) * spread;
            node.y = centers[cid].y + (Math.random() - 0.5) * spread;
        }
    }
}

function initClusterHulls() {
    var clusterToNodes = (typeof graphData !== 'undefined' && graphData) ? graphData.clusterToNodes : (typeof window.clusterToNodes !== 'undefined' ? window.clusterToNodes : {});
    if (!clusterToNodes || Object.keys(clusterToNodes).length === 0) return;
    network.on('afterDrawing', function(ctx) { drawClusterHulls(ctx); });
}
"""


def get_full_css() -> str:
    """Get complete CSS including issue and TODO styles."""
    return BASE_CSS + "\n" + ISSUE_BADGE_CSS + "\n" + TODO_BADGE_CSS + "\n" + CLUSTER_CSS


def get_full_js() -> str:
    """Get complete JavaScript for graph functionality."""
    return "\n".join([RENDER_JS, FILTER_JS, ISSUE_FILTER_JS, TODO_FILTER_JS, TOOLTIP_JS, PANEL_JS, RESIZE_JS, TIMELINE_JS, CLUSTER_JS])


def build_static_html(
    nodes_json: str,
    edges_json: str,
    memories_json: str,
    tag_to_nodes_json: str,
    section_to_nodes_json: str,
    path_to_nodes_json: str,
    status_to_nodes_json: str,
    issue_category_to_nodes_json: str,
    todo_status_to_nodes_json: str,
    todo_category_to_nodes_json: str,
    legend_html: str,
    sections_html: str,
    issues_legend_html: str,
    todos_legend_html: str,
    duplicate_ids_json: str = "[]",
    node_timestamps_json: str = "{}",
    min_date: str = "",
    max_date: str = "",
    version: str = "",
    cluster_to_nodes_json: str = "{}",
    cluster_colors_json: str = "{}",
    cluster_meta_json: str = "{}",
) -> str:
    """Build complete static HTML for export."""
    css = get_full_css()
    js = get_full_js()

    # Build duplicate-memory legend HTML if there are duplicates
    import json
    duplicate_ids = json.loads(duplicate_ids_json)
    duplicates_legend_html = ""
    if duplicate_ids:
        duplicates_legend_html = f'''<div id="duplicates-legend">
<div class="legend-item" onclick="filterByDuplicates()">
<span class="legend-color" style="background:#a855f7;border:2px solid #f85149;"></span>
Duplicate memories ({len(duplicate_ids)})</div></div>'''

    return f'''<!DOCTYPE html>
<html>
<head>
    <title>Memory Knowledge Graph</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css" rel="stylesheet">
    <style>{css}</style>
</head>
<body>
    <div id="graph"></div>
    <div id="resize-handle"></div>
    <div id="panel">
        <span class="close" onclick="closePanel()">&times;</span>
        <div id="panel-tabs">
            <span class="tab active" onclick="switchTab('detail')">Details</span>
            <span class="tab" onclick="switchTab('timeline')">Timeline</span>
            <span class="tab" onclick="switchTab('history')">History</span>
        </div>
        <div id="tab-detail" class="active">
            <h2 id="panel-title">Memory #</h2>
            <div class="meta" id="panel-meta"></div>
            <div class="tags" id="panel-tags"></div>
            <div class="content" id="panel-content"></div>
        </div>
        <div id="tab-timeline">
            <div id="timeline-filter"><button class="filter-btn" onclick="filterTimeline('favorites')">&#9733; Favorites</button><button class="filter-btn active" onclick="filterTimeline('all')">Show All</button></div>
            <div id="timeline-list"></div>
        </div>
        <div id="tab-history">
            <div id="history-list"><div style="padding:20px;color:#8b949e;">History is only available on the live server.</div></div>
        </div>
    </div>
    <div id="legend"><b>Tags</b>{legend_html}{issues_legend_html}{todos_legend_html}{duplicates_legend_html}<div class="reset" onclick="resetFilter()">Show All</div></div>
    <div id="sections"><b>Sections</b>{sections_html}</div>
    <div id="timeline-container" style="display:none;">
        <div id="timeline-label">
            <span class="title">Timeline</span>
            <span id="timeline-current" class="date-range">Drag to filter by time</span>
            <span class="reset" onclick="resetTimeline()" style="cursor:pointer;color:#58a6ff;">Reset</span>
        </div>
        <input type="range" id="timeline-slider" min="0" max="100" value="100" oninput="onTimelineChange(this.value)">
        <div id="timeline-dates">
            <span id="timeline-min-date">Oldest</span>
            <span id="timeline-max-date">Newest</span>
        </div>
    </div>
    <div id="help">Click tag/section to filter | Click node to view | Scroll to zoom | Drag timeline to filter by date</div>
    <div id="version">v{version}</div>
    <div id="node-tooltip"></div>
    <script>
        var memoriesData = {memories_json};
        var tagToNodes = {tag_to_nodes_json};
        var sectionToNodes = {section_to_nodes_json};
        var subsectionToNodes = {path_to_nodes_json};
        var statusToNodes = {status_to_nodes_json};
        var issueCategoryToNodes = {issue_category_to_nodes_json};
        var todoStatusToNodes = {todo_status_to_nodes_json};
        var todoCategoryToNodes = {todo_category_to_nodes_json};
        var duplicateIds = {duplicate_ids_json};
        var duplicateSet = new Set(duplicateIds);
        var clusterToNodes = {cluster_to_nodes_json};
        var clusterColors = {cluster_colors_json};
        var clusterMeta = {cluster_meta_json};
        var allNodes = {nodes_json};
        var allEdges = {edges_json}.map(function(e) {{
            // Color edges between duplicates red
            if (duplicateSet.has(e.from) && duplicateSet.has(e.to)) {{
                return Object.assign({{}}, e, {{ color: {{ color: '#f85149', opacity: 0.8 }} }});
            }}
            return e;
        }});
        var currentFilter = null;
        var graphData = {{ nodes: allNodes, edges: allEdges, statusToNodes: statusToNodes, issueCategoryToNodes: issueCategoryToNodes, todoStatusToNodes: todoStatusToNodes, todoCategoryToNodes: todoCategoryToNodes, duplicateIds: duplicateIds, clusterToNodes: clusterToNodes, clusterColors: clusterColors, clusterMeta: clusterMeta }};
        // Show clusters panel if data exists
        if (Object.keys(clusterToNodes).length > 0) {{
            document.getElementById('clusters').style.display = '';
        }}

        {js}

        // Initialize graph
        seedClusterPositions(allNodes);
        var nodes = new vis.DataSet(allNodes);
        var edges = new vis.DataSet(allEdges);
        var container = document.getElementById("graph");
        var data = {{ nodes: nodes, edges: edges }};
        var options = {{
            nodes: {{ shape: "dot", size: 16, font: {{ color: "#c9d1d9", size: 11 }}, borderWidth: 2 }},
            edges: {{ color: {{ color: "#30363d", opacity: 0.6 }}, smooth: {{ type: "continuous" }} }},
            physics: {{ barnesHut: {{ gravitationalConstant: -2000, springLength: 95, springConstant: 0.04, damping: 0.3, avoidOverlap: 0.3 }} }},
            interaction: {{ hover: true, tooltipDelay: 99999 }}
        }};
        var network = new vis.Network(container, data, options);

        network.on("click", function(params) {{
            hideNodeTooltip();
            if (params.nodes.length > 0) {{
                var nodeId = params.nodes[0];
                var mem = memoriesData[nodeId];
                if (mem) showPanel(mem);
            }}
        }});

        network.on("hoverNode", function(params) {{
            showNodeTooltip(params.node, params.pointer);
        }});

        network.on("blurNode", function() {{
            hideNodeTooltip();
        }});

        // Initialize timeline
        var nodeTimestamps = {node_timestamps_json};
        initTimeline(nodeTimestamps, "{min_date}", "{max_date}");

        // Initialize cluster hulls
        initClusterHulls();
    </script>
</body>
</html>'''
