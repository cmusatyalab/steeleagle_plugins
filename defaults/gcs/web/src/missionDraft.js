// The Mission Builder draft (canvas plus map features), persisted in
// localStorage so it survives page reloads and GCS restarts (tab switches
// never lost it: PlanPage stays mounted, just hidden). Like
// the Flow Builder draft it's a convenience, not storage -- the exported
// DSL and GeoJSON are the saved mission. Unlike them it keeps node
// positions, which a DSL round-trip re-lays out.
export const MISSION_DRAFT_KEY = 'steeleagle-mission-draft';
export const MISSION_DRAFT_VERSION = 1;

const EMPTY_FEATURES = JSON.stringify({ type: 'FeatureCollection', features: [] });

// Older dropdowns could store their selectable empty row instead of null.
const legacyEmptyOptions = new Set([
    '(no values)',
    '(add a matching engine on the Flow Builder tab)',
    '(draw an area on the Map tab)',
]);

function reviveDraftValue(_key, value) {
    if (value?.value === null && legacyEmptyOptions.has(value.label) && Object.keys(value).length === 2) {
        return null;
    }
    return value;
}

export function emptyMissionDraft() {
    return { nodes: [], edges: [], eventInstances: [], startNodeId: null, features: EMPTY_FEATURES };
}

// Returns the stored draft, or an empty one when nothing is stored or what
// is stored is corrupt or from another draft version.
export function loadMissionDraft() {
    try {
        const raw = localStorage.getItem(MISSION_DRAFT_KEY);
        if (!raw) return emptyMissionDraft();
        const d = JSON.parse(raw, reviveDraftValue);
        if (d?.version !== MISSION_DRAFT_VERSION) return emptyMissionDraft();
        if (!Array.isArray(d.nodes) || !Array.isArray(d.edges) || !Array.isArray(d.eventInstances)) {
            return emptyMissionDraft();
        }
        return {
            nodes: d.nodes,
            edges: d.edges,
            eventInstances: d.eventInstances,
            startNodeId: typeof d.startNodeId === 'string' ? d.startNodeId : null,
            features: typeof d.features === 'string' ? d.features : EMPTY_FEATURES,
        };
    } catch {
        return emptyMissionDraft();
    }
}

// `draft` is {nodes, edges, eventInstances, startNodeId, features}, with
// nodes already stripped of non-serializable data (see snapshotCurrent).
export function saveMissionDraft(draft) {
    try {
        localStorage.setItem(MISSION_DRAFT_KEY, JSON.stringify({ version: MISSION_DRAFT_VERSION, ...draft }));
    } catch {
        // storage full or blocked; the mission still lives in page state
    }
}

// The highest N among `node-N` ids, so freshly dropped nodes can be
// numbered past the restored ones instead of colliding with them.
export function highestNodeNumber(nodes) {
    return nodes.reduce((max, n) => {
        const m = /^node-(\d+)$/.exec(n.id);
        return m ? Math.max(max, Number(m[1])) : max;
    }, 0);
}
