// The Metrics page's pure parts: laying out a vehicle's running flow, and
// formatting its numbers.
import dagre from '@dagrejs/dagre';

// The windows the numbers can cover, as the GCS's /api/metrics/flow takes them.
export const WINDOWS = ['1m', '5m', '15m', '1h'];
export const DEFAULT_WINDOW = WINDOWS[0];
// How often the page asks for fresh numbers: Prometheus's scrape interval.
export const POLL_INTERVAL_MS = 5000;

// Room each node takes in the layout, and the gaps between them.
export const NODE_WIDTH = 190;
export const NODE_HEIGHT = 110;
const RANK_GAP = 80;
const NODE_GAP = 30;

const MS_PER_S = 1000;
const PERCENT = 100;
// Below this many milliseconds, a time shows a decimal (e.g. "0.2 ms").
const WHOLE_MS_FROM = 1;
const NONE = '–';

// Positions (top-left corners) of the nodes, left to right along the edges.
export function layout(nodes, edges) {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: 'LR', ranksep: RANK_GAP, nodesep: NODE_GAP });
    g.setDefaultEdgeLabel(() => ({}));
    for (const n of nodes) g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
    for (const e of edges) g.setEdge(e.from, e.to);
    dagre.layout(g);
    const at = {};
    for (const n of nodes) {
        const { x, y } = g.node(n.id);
        at[n.id] = { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 };
    }
    return at;
}

export function formatSeconds(s) {
    if (s == null) return NONE;
    if (s >= 1) return `${s.toFixed(1)} s`;
    const ms = s * MS_PER_S;
    return ms < WHOLE_MS_FROM ? `${ms.toFixed(1)} ms` : `${Math.round(ms)} ms`;
}

export function formatShare(x) {
    return x == null ? NONE : `${Math.round(x * PERCENT)}%`;
}

export function formatRate(r) {
    return r == null ? NONE : `${r.toFixed(1)}/s`;
}

// React Flow's nodes and edges for a view from /api/metrics/flow.
export function toReactFlow(view) {
    const at = layout(view.nodes, view.edges);
    return {
        nodes: view.nodes.map((n) => ({
            id: n.id,
            type: 'metrics',
            position: at[n.id],
            data: { kind: n.kind, label: n.label, host: n.host, metrics: n.metrics, bottleneck: n.bottleneck },
        })),
        edges: view.edges.map((e) => ({ id: `${e.from}->${e.to}`, source: e.from, target: e.to })),
    };
}
