"""Graph data generation and transformation logic."""

import json
import os
from datetime import datetime, timedelta
from importlib.metadata import version as get_version
from typing import Any, Dict, List, Optional


def _get_memora_version() -> str:
    try:
        return get_version("memora")
    except Exception:
        return ""

# Stale threshold for closed issues/TODOs (in days)
# Closed items older than this will appear gray and smaller
STALE_DAYS = int(os.getenv("MEMORA_STALE_DAYS", "30"))

from ..storage import (  # noqa: E402
    connect,
    detect_clusters,
    get_crossrefs,
    get_memory,
    list_memories,
    rebuild_crossrefs,
)
from .issues import (  # noqa: E402
    TAG_COLORS,
    build_issue_category_to_nodes,
    build_issue_legend_html,
    build_status_to_nodes,
    get_issue_node_style,
    is_issue,
)
from .templates import build_static_html  # noqa: E402
from .todos import (  # noqa: E402
    build_todo_category_to_nodes,
    build_todo_legend_html,
    build_todo_status_to_nodes,
    get_todo_node_style,
    is_todo,
)

# Similarity threshold for duplicate detection
DUPLICATE_THRESHOLD = 0.85

# Stale styling
STALE_COLOR = "#8b949e"  # Gray
STALE_SIZE_FACTOR = 0.7  # Reduce size to 70%


def _is_stale_closed(metadata: Optional[Dict], updated_at: Optional[str], created_at: Optional[str]) -> bool:
    """Check if a closed issue/TODO is stale (older than STALE_DAYS threshold).

    Uses updated_at if available, otherwise falls back to created_at.
    Only applies to closed issues/TODOs.
    """
    if not metadata:
        return False

    # Only check issues and TODOs that are closed
    mem_type = metadata.get("type")
    status = metadata.get("status")

    if mem_type not in ("issue", "todo"):
        return False
    if status != "closed":
        return False

    # Get the reference date (prefer updated_at, fall back to created_at)
    date_str = updated_at or created_at
    if not date_str:
        return False

    try:
        # Parse the date string (format: "2025-12-23 19:59:31")
        ref_date = datetime.strptime(date_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
        threshold = datetime.now() - timedelta(days=STALE_DAYS)
        return ref_date < threshold
    except (ValueError, TypeError):
        return False


def is_section(metadata: Optional[Dict]) -> bool:
    """Check if a memory is a section header based on metadata."""
    if not metadata:
        return False
    return metadata.get("type") == "section"


def _find_duplicate_ids(conn, memories: List[Dict]) -> set:
    """Find memory IDs that have duplicates (similarity >= threshold).

    A memory is marked as duplicate if it has a cross-reference with
    score >= DUPLICATE_THRESHOLD to another memory in the current view.
    Section memories are excluded from duplicate detection.
    """
    # Exclude section memories from duplicate detection
    non_section_memories = [m for m in memories if not is_section(m.get("metadata"))]
    memory_ids = {m["id"] for m in non_section_memories}
    duplicate_ids = set()

    for m in non_section_memories:
        for ref in get_crossrefs(conn, m["id"]):
            if ref.get("score", 0) >= DUPLICATE_THRESHOLD:
                # Only mark if the related memory is also in our view
                if ref["id"] in memory_ids:
                    duplicate_ids.add(m["id"])
                    duplicate_ids.add(ref["id"])

    return duplicate_ids


def _expand_r2_urls(metadata: Optional[Dict]) -> Dict:
    """Expand R2 URLs in metadata for display."""
    if not metadata:
        return {}

    meta = dict(metadata)
    if meta.get("images"):
        from ..image_storage import expand_r2_url

        expanded_images = []
        for img in meta["images"]:
            if isinstance(img, dict) and img.get("src"):
                src = img["src"]
                if src.startswith("r2://") or src.startswith("/r2/"):
                    src = expand_r2_url(
                        src.replace("/r2/", "r2://") if src.startswith("/r2/") else src,
                        use_proxy=True,
                    )
                expanded_images.append({**img, "src": src})
            else:
                expanded_images.append(img)
        meta["images"] = expanded_images

    return meta


def _build_tag_colors(memories: List[Dict]) -> Dict[str, str]:
    """Build tag -> color mapping from memories."""
    tag_colors = {}
    for m in memories:
        tags = m.get("tags", [])
        primary_tag = tags[0] if tags else "untagged"
        if primary_tag not in tag_colors:
            tag_colors[primary_tag] = TAG_COLORS[len(tag_colors) % len(TAG_COLORS)]
    return tag_colors


def _count_connections(edges: List[Dict]) -> Dict[int, int]:
    """Count connections per node from edge list."""
    counts: Dict[int, int] = {}
    for edge in edges:
        from_id = edge["from"]
        to_id = edge["to"]
        counts[from_id] = counts.get(from_id, 0) + 1
        counts[to_id] = counts.get(to_id, 0) + 1
    return counts


def _build_nodes(
    memories: List[Dict],
    tag_colors: Dict[str, str],
    connection_counts: Optional[Dict[int, int]] = None,
    duplicate_ids: Optional[set] = None,
) -> List[Dict]:
    """Build vis.js node objects from memories.

    Section memories are excluded from the graph.
    """
    import math

    if duplicate_ids is None:
        duplicate_ids = set()

    nodes = []
    for m in memories:
        # Skip section memories - they are not visible in the graph
        if is_section(m.get("metadata")):
            continue
        tags = m.get("tags", [])
        primary_tag = tags[0] if tags else "untagged"
        meta = m.get("metadata") or {}

        content = m["content"]
        # Get first line or first 60 chars for headline, strip markdown headers
        first_line = content.split("\n")[0].lstrip("#").strip()[:60]
        headline = first_line.replace('"', "'").replace("\\", "")
        label = content[:35].replace("\n", " ").replace("#", "").replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "").strip().replace('"', "'").replace("\\", "")

        # Calculate node size based on connections (like Connected Papers)
        connections = connection_counts.get(m["id"], 0) if connection_counts else 0
        # Use logarithmic scaling: base size 12, grows with connections
        # Min size 12, max size ~40
        node_size = 12 + min(28, int(math.log1p(connections) * 8))
        # Mass affects physics: higher mass = more central, lower mass = pushed to edges
        # Nodes with 0 connections have mass 0.5, highly connected nodes up to mass 3
        node_mass = 0.5 + min(2.5, math.log1p(connections) * 0.8)

        # Build title with type indicator
        type_label = ""
        if is_issue(meta):
            type_label = " - Issue"
        elif is_todo(meta):
            type_label = " - TODO"

        node = {
            "id": m["id"],
            "label": label + "..." if len(content) > 35 else label,
            "title": f"#{m['id']}{type_label}\n{headline}",
            "color": tag_colors[primary_tag],
            "size": node_size,
            "mass": node_mass,
        }

        # Apply issue-specific styling
        issue_style = get_issue_node_style(meta)
        if issue_style:
            node.update(issue_style)

        # Apply TODO-specific styling
        todo_style = get_todo_node_style(meta)
        if todo_style:
            node.update(todo_style)

        # Apply duplicate indicator - red border
        if m["id"] in duplicate_ids:
            node["color"] = {
                "background": node.get("color", "#a855f7"),
                "border": "#f85149",
            }
            node["borderWidth"] = 3

        # Apply stale styling for old closed issues/TODOs
        if _is_stale_closed(meta, m.get("updated_at"), m.get("created_at")):
            node["color"] = STALE_COLOR
            node["size"] = int(node.get("size", 12) * STALE_SIZE_FACTOR)

        nodes.append(node)

    return nodes


def _build_tag_to_nodes(memories: List[Dict]) -> Dict[str, List[int]]:
    """Build tag -> node IDs mapping. Section memories are excluded."""
    tag_to_nodes: Dict[str, List[int]] = {}
    for m in memories:
        # Skip section memories - they're not visible in graph
        if is_section(m.get("metadata")):
            continue
        for tag in m.get("tags", []):
            if tag not in tag_to_nodes:
                tag_to_nodes[tag] = []
            tag_to_nodes[tag].append(m["id"])
    return tag_to_nodes


def _build_section_mappings(memories: List[Dict]) -> tuple:
    """Build section and subsection -> node IDs mappings.

    Returns (section_to_nodes, path_to_nodes) tuple.
    Issues, TODOs, and section placeholders are excluded.
    """
    section_to_nodes: Dict[str, List[int]] = {}
    path_to_nodes: Dict[str, List[int]] = {}

    for m in memories:
        meta = m.get("metadata") or {}

        # Skip issues, TODOs, and section placeholders
        if is_issue(meta) or is_todo(meta) or is_section(meta):
            continue

        hierarchy = meta.get("hierarchy", {})
        hierarchy_path = hierarchy.get("path", []) if isinstance(hierarchy, dict) else []

        if hierarchy_path and len(hierarchy_path) >= 1:
            section = hierarchy_path[0]
            parts = hierarchy_path[1:]
        else:
            section = meta.get("section", "Uncategorized")
            subsection = meta.get("subsection", "")
            parts = subsection.split("/") if subsection else []

        if section not in section_to_nodes:
            section_to_nodes[section] = []
        section_to_nodes[section].append(m["id"])

        if parts:
            for i in range(len(parts)):
                partial_path = "/".join(parts[: i + 1])
                full_key = f"{section}/{partial_path}"
                if full_key not in path_to_nodes:
                    path_to_nodes[full_key] = []
                path_to_nodes[full_key].append(m["id"])

    return section_to_nodes, path_to_nodes


def _build_edges(conn, memories: List[Dict], min_score: float) -> List[Dict]:
    """Build vis.js edge objects from crossrefs."""
    edges = []
    seen = set()
    edge_id = 0
    for m in memories:
        for ref in get_crossrefs(conn, m["id"]):
            edge_key = tuple(sorted([m["id"], ref["id"]]))
            if edge_key not in seen and ref.get("score", 0) > min_score:
                seen.add(edge_key)
                edges.append({"id": edge_id, "from": m["id"], "to": ref["id"]})
                edge_id += 1
    return edges


def _build_timeline_data(memories: List[Dict]) -> tuple:
    """Build timeline data from memories.

    Returns:
        (node_timestamps, min_date, max_date) tuple
        - node_timestamps: dict mapping node ID to created_at timestamp
        - min_date: earliest date string
        - max_date: latest date string
    """
    node_timestamps: Dict[int, str] = {}
    dates = []

    for m in memories:
        # Skip section memories
        if is_section(m.get("metadata")):
            continue

        created_at = m.get("created_at")
        if created_at:
            node_timestamps[m["id"]] = created_at
            dates.append(created_at)

    if not dates:
        return {}, "", ""

    dates.sort()
    return node_timestamps, dates[0], dates[-1]


CLUSTER_COLORS = [
    "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
    "#ff922b", "#cc5de8", "#20c997", "#339af0",
    "#f06595", "#a9e34b", "#22b8cf", "#845ef7",
]


def _build_cluster_data(
    conn, memories: List[Dict], min_score: float = 0.5
) -> Dict[str, Any]:
    """Build cluster mappings using Louvain community detection.

    Returns dict with clusterToNodes, nodeToCluster, clusterColors, clusterMeta.
    """
    # Filter out section memories
    non_section_ids = [
        m["id"] for m in memories if not is_section(m.get("metadata"))
    ]

    clusters = detect_clusters(
        conn, min_cluster_size=3, min_score=min_score, algorithm="louvain"
    )

    if not clusters:
        return {
            "clusterToNodes": {},
            "nodeToCluster": {},
            "clusterColors": {},
            "clusterMeta": {},
        }

    cluster_to_nodes: Dict[str, List[int]] = {}
    node_to_cluster: Dict[str, int] = {}
    cluster_colors: Dict[str, str] = {}
    cluster_meta: Dict[str, Dict] = {}

    non_section_set = set(non_section_ids)

    for c in clusters:
        cid = str(c["cluster_id"])
        # Only include non-section memories that are in the graph
        members = [mid for mid in c["memory_ids"] if mid in non_section_set]
        if len(members) < 3:
            continue

        cluster_to_nodes[cid] = members
        color = CLUSTER_COLORS[(c["cluster_id"] - 1) % len(CLUSTER_COLORS)]
        cluster_colors[cid] = color

        for mid in members:
            node_to_cluster[str(mid)] = c["cluster_id"]

        # Build a human-readable label from top tags
        label_tags = [t for t in c.get("top_tags", []) if "/" not in t][:2]
        if not label_tags:
            label_tags = [t.split("/")[-1] for t in c.get("top_tags", [])[:2]]
        label = ", ".join(label_tags) if label_tags else f"Cluster {cid}"

        cluster_meta[cid] = {
            "size": len(members),
            "top_tags": c.get("top_tags", []),
            "label": label,
        }

    return {
        "clusterToNodes": cluster_to_nodes,
        "nodeToCluster": node_to_cluster,
        "clusterColors": cluster_colors,
        "clusterMeta": cluster_meta,
    }


def _build_cluster_legend_html(
    cluster_meta: Dict[str, Dict], cluster_colors: Dict[str, str]
) -> str:
    """Build HTML for cluster legend panel."""
    html = ""
    for cid, meta in cluster_meta.items():
        color = cluster_colors.get(cid, "#8b949e")
        label = meta["label"]
        size = meta["size"]
        html += (
            f'<div class="cluster-item" data-cluster="{cid}" '
            f"onclick=\"filterByCluster('{cid}')\">"
            f'<span class="cluster-color" style="background:{color}"></span>'
            f"{label} ({size})</div>"
        )
    if cluster_meta:
        html += '<label class="hull-toggle"><input type="checkbox" onchange="toggleClusterHulls(this)"> Show boundaries</label>'
    return html


def _build_legend_html(tag_colors: Dict[str, str]) -> str:
    """Build HTML for tag legend."""
    return "".join(
        f'<div class="legend-item" data-tag="{t}" onclick="filterByTag(\'{t}\')">'
        f'<span class="legend-color" style="background:{c}"></span>{t}</div>'
        for t, c in list(tag_colors.items())[:12]
    )


def _build_sections_html(
    section_to_nodes: Dict[str, List[int]], path_to_nodes: Dict[str, List[int]]
) -> str:
    """Build HTML for sections hierarchy."""
    sections_html = ""
    for section, node_ids in section_to_nodes.items():
        sections_html += (
            f'<div class="section-item" data-section="{section}" '
            f"onclick=\"filterBySection('{section}')\">{section} ({len(node_ids)})</div>"
        )

        section_paths = sorted(
            [k for k in path_to_nodes.keys() if k.startswith(section + "/")]
        )
        rendered_paths = set()

        for full_path in section_paths:
            sub_path = full_path[len(section) + 1 :]
            parts = sub_path.split("/")

            for i, part in enumerate(parts):
                partial = "/".join(parts[: i + 1])
                render_key = f"{section}/{partial}"

                if render_key not in rendered_paths:
                    rendered_paths.add(render_key)
                    indent = "&nbsp;&nbsp;" * i
                    count = len(path_to_nodes.get(render_key, []))
                    sections_html += (
                        f'<div class="subsection-item" data-subsection="{render_key}" '
                        f"onclick=\"filterBySubsection('{render_key}')\" "
                        f'style="padding-left:{8 + i*12}px;">{indent}└ {part} ({count})</div>'
                    )

    return sections_html


def get_graph_data(min_score: float = 0.40, rebuild: bool = False) -> Dict[str, Any]:
    """Get graph nodes, edges, and metadata for API response.

    Args:
        min_score: Minimum similarity score for edges
        rebuild: If True, rebuild crossrefs (slow). If False, use existing.

    Returns:
        Dict with nodes, edges, and various mappings.
    """
    conn = connect()
    try:
        memories = list_memories(conn, None, None, None, 0, None, None, None, None, None)
        if not memories:
            return {"error": "no_memories", "message": "No memories to visualize"}

        if rebuild:
            rebuild_crossrefs(conn)

        # Build edges first to calculate connection counts for node sizing
        edges = _build_edges(conn, memories, min_score)
        connection_counts = _count_connections(edges)

        # Find duplicates (similarity >= 0.7)
        duplicate_ids = _find_duplicate_ids(conn, memories)

        tag_colors = _build_tag_colors(memories)
        nodes = _build_nodes(memories, tag_colors, connection_counts, duplicate_ids)
        tag_to_nodes = _build_tag_to_nodes(memories)
        section_to_nodes, path_to_nodes = _build_section_mappings(memories)
        status_to_nodes = build_status_to_nodes(memories)
        issue_category_to_nodes = build_issue_category_to_nodes(memories)
        todo_status_to_nodes = build_todo_status_to_nodes(memories)
        todo_category_to_nodes = build_todo_category_to_nodes(memories)

        # Build timeline data
        node_timestamps, min_date, max_date = _build_timeline_data(memories)

        # Build cluster data using Louvain community detection
        cluster_data = _build_cluster_data(conn, memories)

        result = {
            "nodes": nodes,
            "edges": edges,
            "tagColors": tag_colors,
            "tagToNodes": tag_to_nodes,
            "sectionToNodes": section_to_nodes,
            "subsectionToNodes": path_to_nodes,
            "statusToNodes": status_to_nodes,
            "issueCategoryToNodes": issue_category_to_nodes,
            "todoStatusToNodes": todo_status_to_nodes,
            "todoCategoryToNodes": todo_category_to_nodes,
            "duplicateIds": list(duplicate_ids),
            "nodeTimestamps": node_timestamps,
            "minDate": min_date,
            "maxDate": max_date,
        }
        result.update(cluster_data)
        return result

    finally:
        conn.close()


def get_memory_for_api(memory_id: int) -> Dict[str, Any]:
    """Get a single memory with expanded R2 URLs for API response."""
    conn = connect()
    try:
        m = get_memory(conn, memory_id)
        if not m:
            return {"error": "not_found"}

        meta = _expand_r2_urls(m.get("metadata"))

        return {
            "id": m["id"],
            "content": m["content"],
            "tags": m.get("tags", []),
            "created": m.get("created_at", ""),
            "updated": m.get("updated_at"),
            "metadata": meta,
        }
    finally:
        conn.close()


def export_graph_html(
    output_path: Optional[str] = None, min_score: float = 0.40
) -> Dict[str, Any]:
    """Generate static HTML knowledge graph visualization.

    Args:
        output_path: Path to save HTML file, or None to return HTML in result.
        min_score: Minimum similarity score for edges.

    Returns:
        Dict with node/edge counts, tags, and optionally path or html.
    """
    conn = connect()
    try:
        memories = list_memories(conn, None, None, None, 0, None, None, None, None, None)
        if not memories:
            return {"error": "no_memories", "message": "No memories to visualize"}

        rebuild_crossrefs(conn)

        # Build edges first to calculate connection counts for node sizing
        edges = _build_edges(conn, memories, min_score)
        connection_counts = _count_connections(edges)

        # Find duplicates (similarity >= 0.7)
        duplicate_ids = _find_duplicate_ids(conn, memories)

        tag_colors = _build_tag_colors(memories)
        nodes = _build_nodes(memories, tag_colors, connection_counts, duplicate_ids)
        tag_to_nodes = _build_tag_to_nodes(memories)
        section_to_nodes, path_to_nodes = _build_section_mappings(memories)
        status_to_nodes = build_status_to_nodes(memories)
        issue_category_to_nodes = build_issue_category_to_nodes(memories)
        todo_status_to_nodes = build_todo_status_to_nodes(memories)
        todo_category_to_nodes = build_todo_category_to_nodes(memories)

        # Build timeline data
        node_timestamps, min_date, max_date = _build_timeline_data(memories)

        # Build memories data for inline display
        memories_data = {}
        for m in memories:
            meta = _expand_r2_urls(m.get("metadata"))
            memories_data[m["id"]] = {
                "id": m["id"],
                "tags": m.get("tags", []),
                "created": m.get("created_at", ""),
                "updated": m.get("updated_at"),
                "content": m["content"],
                "metadata": meta,
            }

        # Build cluster data using Louvain community detection
        cluster_data = _build_cluster_data(conn, memories)

        # Build HTML components
        legend_html = _build_legend_html(tag_colors)
        sections_html = _build_sections_html(section_to_nodes, path_to_nodes)
        issues_legend_html = build_issue_legend_html(status_to_nodes, issue_category_to_nodes)
        todos_legend_html = build_todo_legend_html(todo_status_to_nodes, todo_category_to_nodes)
        html = build_static_html(
            nodes_json=json.dumps(nodes),
            edges_json=json.dumps(edges),
            memories_json=json.dumps(memories_data),
            tag_to_nodes_json=json.dumps(tag_to_nodes),
            section_to_nodes_json=json.dumps(section_to_nodes),
            path_to_nodes_json=json.dumps(path_to_nodes),
            status_to_nodes_json=json.dumps(status_to_nodes),
            issue_category_to_nodes_json=json.dumps(issue_category_to_nodes),
            todo_status_to_nodes_json=json.dumps(todo_status_to_nodes),
            todo_category_to_nodes_json=json.dumps(todo_category_to_nodes),
            legend_html=legend_html,
            sections_html=sections_html,
            issues_legend_html=issues_legend_html,
            todos_legend_html=todos_legend_html,
            duplicate_ids_json=json.dumps(list(duplicate_ids)),
            node_timestamps_json=json.dumps(node_timestamps),
            min_date=min_date,
            max_date=max_date,
            version=_get_memora_version(),
            cluster_to_nodes_json=json.dumps(cluster_data["clusterToNodes"]),
            cluster_colors_json=json.dumps(cluster_data["clusterColors"]),
            cluster_meta_json=json.dumps(cluster_data["clusterMeta"]),
        )

        result = {
            "nodes": len(nodes),
            "edges": len(edges),
            "tags": list(tag_colors.keys()),
        }

        if output_path is not None:
            with open(output_path, "w") as f:
                f.write(html)
            result["path"] = output_path
        else:
            result["html"] = html

        return result

    finally:
        conn.close()
