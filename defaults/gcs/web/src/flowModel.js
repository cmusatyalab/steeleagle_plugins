// Flow documents for the Plan page's Flow Builder -- see
// docs/superpowers/specs/2026-10-16-gcs-flow-builder-design.md.
//
// A flow is bound to one vehicle (its full service name); its producers are
// that vehicle's streams and its engines are engine services on the
// vehicle's daemon ("local") or the orchestrator ("remote"). Producers and
// engines are named "{host}.{name}" as in the flow template, where host is a
// daemon's hostname or address. The document stores references only (host
// and stream, template name); types and events are looked up in the catalog
// (flowCatalog.js) whenever they're needed, so a flow loaded after the setup
// changed shows what no longer resolves rather than silently dropping it.
// Every function here is pure: it returns a new flow and never mutates its
// input.
import {
    engineLocation, findDaemon, findVehicle, hostLabel, resolveNode, serviceName, shortType,
    splitTemplateName, templateName,
} from './flowCatalog.js';
import { daemonRequest, describeFailure } from './daemonApi.js';
import { daemonOf, displayName } from './vehicleNames.js';

export const SCHEMA_VERSION = 3;
export const OLD_VERSION_MESSAGE = "This flow was saved by an older Flow Builder and can't be loaded; rebuild it or load its TOML export";
export const DEFAULT_TIMEOUT = '1s';
export const DEFAULT_TOKENS = 1;

export function emptyFlow() {
    return { schemaVersion: SCHEMA_VERSION, name: 'untitled-flow', vehicle: null, nodes: [], edges: [] };
}

// Ids are a kind prefix plus a number ("p1", "e2", "x3"), one past the
// highest in use, so they stay short and readable in exported JSON.
function nextId(items, prefix) {
    const used = items
        .map((item) => item.id)
        .filter((id) => id.startsWith(prefix))
        .map((id) => Number(id.slice(prefix.length)))
        .filter(Number.isInteger);
    return `${prefix}${Math.max(0, ...used) + 1}`;
}

export function nodeById(flow, id) {
    return flow.nodes.find((n) => n.id === id) ?? null;
}

// A node's template name: "{vehicle}.{stream}" for a producer, where the
// vehicle is the flow's (only a label: each Deploy target uses its own stream
// of that name), and "{host}.{name}" for an engine.
export function nodeLabel(flow, node) {
    if (node.kind === 'producer') return flow.vehicle ? `${displayName(flow.vehicle)}.${node.stream}` : node.stream;
    return node.engine;
}

// The name a node has in the flow template: the same template name the
// builder shows.
export function exportName(flow, catalog, node) {
    return nodeLabel(flow, node);
}

// The producer (other than `exceptId`) on a primary camera, if any. Primary
// cameras can't stream alongside each other, so a flow has at most one.
function primaryProducer(flow, catalog, exceptId) {
    const vehicle = findVehicle(catalog, flow.vehicle);
    const primary = new Set(vehicle?.streams.filter((s) => s.primary).map((s) => s.name) ?? []);
    return flow.nodes.find((n) => n.kind === 'producer' && n.id !== exceptId && primary.has(n.stream));
}

// The streams a producer can be on: the bound vehicle's streams no other
// producer uses (plus `nodeId`'s own, even if the catalog lacks it), with
// primary cameras disabled while another producer is on one.
export function streamChoices(flow, catalog, nodeId = null) {
    const vehicle = findVehicle(catalog, flow.vehicle);
    const own = nodeId && nodeById(flow, nodeId)?.stream;
    const others = new Set(flow.nodes.filter((n) => n.kind === 'producer' && n.id !== nodeId).map((n) => n.stream));
    const blocker = primaryProducer(flow, catalog, nodeId);
    const choices = (vehicle?.streams ?? [])
        .filter((s) => !others.has(s.name))
        .map((s) => {
            const disabled = Boolean(s.primary && blocker);
            return {
                name: s.name,
                primary: Boolean(s.primary),
                disabled,
                reason: disabled ? `${nodeLabel(flow, blocker)} is already streaming a primary camera` : null,
            };
        });
    if (own && !choices.some((c) => c.name === own)) {
        choices.unshift({ name: own, primary: false, disabled: false, reason: null });
    }
    return choices;
}

// What a new producer could be on: unplaced streams that aren't blocked.
export function availableStreams(flow, catalog) {
    return streamChoices(flow, catalog).filter((c) => !c.disabled).map((c) => c.name);
}

// Whether the catalog engine `service` (a full service name) is on the
// canvas, however its node spells the host.
export function isEnginePlaced(flow, catalog, service) {
    return flow.nodes.some((n) => n.kind === 'engine' && serviceName(catalog, n.engine).name === service);
}

export function addProducer(flow, catalog, stream, position) {
    if (!flow.vehicle) throw new Error('Select a vehicle first');
    if (flow.nodes.some((n) => n.kind === 'producer' && n.stream === stream)) {
        throw new Error(`${displayName(flow.vehicle)}.${stream} is already on the canvas`);
    }
    const node = { id: nextId(flow.nodes, 'p'), kind: 'producer', stream, tokens: DEFAULT_TOKENS, position };
    return { ...flow, nodes: [...flow.nodes, node] };
}

// `service` is the palette's catalog service name ("100.64.0.2.yolo"); the
// node stores its template name. The engine must run on the vehicle's daemon
// or on the orchestrator.
export function addEngine(flow, catalog, service, position) {
    if (!flow.vehicle) throw new Error('Select a vehicle first');
    const engine = templateName(catalog, service);
    if (engineLocation(catalog, flow.vehicle, engine) === null) {
        throw new Error(`${engine} is neither on ${displayName(flow.vehicle)}'s daemon nor on the orchestrator`);
    }
    if (isEnginePlaced(flow, catalog, service)) throw new Error(`${engine} is already on the canvas`);
    const node = { id: nextId(flow.nodes, 'e'), kind: 'engine', engine, position };
    return { ...flow, nodes: [...flow.nodes, node] };
}

export function updateNode(flow, id, patch) {
    return { ...flow, nodes: flow.nodes.map((n) => (n.id === id ? { ...n, ...patch } : n)) };
}

export function updateEdge(flow, id, patch) {
    return { ...flow, edges: flow.edges.map((e) => (e.id === id ? { ...e, ...patch } : e)) };
}

export function removeNode(flow, id) {
    return {
        ...flow,
        nodes: flow.nodes.filter((n) => n.id !== id),
        edges: flow.edges.filter((e) => e.source !== id && e.target !== id),
    };
}

export function removeEdge(flow, id) {
    return { ...flow, edges: flow.edges.filter((e) => e.id !== id) };
}

// Producers are streams of the bound vehicle, so binding a different
// vehicle drops them (and their edges); engines stay.
export function setVehicle(flow, vehicle) {
    if (flow.vehicle === vehicle) return flow;
    const producers = new Set(flow.nodes.filter((n) => n.kind === 'producer').map((n) => n.id));
    return {
        ...flow,
        vehicle,
        nodes: flow.nodes.filter((n) => !producers.has(n.id)),
        edges: flow.edges.filter((e) => !producers.has(e.source) && !producers.has(e.target)),
    };
}

// --- JSON import/export ----------------------------------------------------

const isObject = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
const isPosition = (v) => isObject(v) && Number.isFinite(v.x) && Number.isFinite(v.y);

function parseNode(n) {
    if (n.kind === 'producer') {
        if (typeof n.stream !== 'string') throw new Error(`node ${n.id}: producer nodes need a stream name`);
        return { id: n.id, kind: 'producer', stream: n.stream, tokens: n.tokens ?? DEFAULT_TOKENS, position: n.position };
    }
    if (n.kind === 'engine') {
        if (typeof n.engine !== 'string') throw new Error(`node ${n.id}: engine nodes need an engine template name`);
        return { id: n.id, kind: 'engine', engine: n.engine, position: n.position };
    }
    throw new Error(`node ${n.id}: unknown kind ${JSON.stringify(n.kind)}`);
}

function parseEdge(e) {
    const base = { id: e.id, kind: e.kind, source: e.source, target: e.target };
    if (e.kind === 'output') return { ...base, timeout: e.timeout ?? DEFAULT_TIMEOUT };
    if (e.kind === 'callback') {
        if (typeof e.event !== 'string') throw new Error(`edge ${e.id}: callback edges need an event`);
        return { ...base, event: e.event };
    }
    throw new Error(`edge ${e.id}: unknown kind ${JSON.stringify(e.kind)}`);
}

// Checks structure only (well-formed references, known kinds); whether the
// flow makes sense against the current catalog is validateFlow's job, so a
// stale flow still imports and shows what's wrong.
export function parseFlowJson(text) {
    let doc;
    try {
        doc = JSON.parse(text);
    } catch (e) {
        throw new Error(`not valid JSON: ${e.message}`);
    }
    if (!isObject(doc)) throw new Error('not a flow document');
    if (doc.schemaVersion === 1 || doc.schemaVersion === 2) throw new Error(OLD_VERSION_MESSAGE);
    if (doc.schemaVersion !== SCHEMA_VERSION) {
        throw new Error(`unsupported schemaVersion ${JSON.stringify(doc.schemaVersion)} (expected ${SCHEMA_VERSION})`);
    }
    if (typeof doc.name !== 'string') throw new Error('name must be a string');
    if (doc.vehicle !== null && typeof doc.vehicle !== 'string') throw new Error('vehicle must be null or a full vehicle name');
    if (!Array.isArray(doc.nodes) || !Array.isArray(doc.edges)) throw new Error('nodes and edges must be arrays');

    const nodeIds = new Set();
    const nodes = doc.nodes.map((n) => {
        if (!isObject(n) || typeof n.id !== 'string') throw new Error('every node needs a string id');
        if (nodeIds.has(n.id)) throw new Error(`duplicate node id ${n.id}`);
        nodeIds.add(n.id);
        if (!isPosition(n.position)) throw new Error(`node ${n.id}: position must be {x, y}`);
        return parseNode(n);
    });
    const edgeIds = new Set();
    const edges = doc.edges.map((e) => {
        if (!isObject(e) || typeof e.id !== 'string') throw new Error('every edge needs a string id');
        if (edgeIds.has(e.id)) throw new Error(`duplicate edge id ${e.id}`);
        edgeIds.add(e.id);
        if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) throw new Error(`edge ${e.id} references a missing node`);
        return parseEdge(e);
    });
    return { schemaVersion: SCHEMA_VERSION, name: doc.name, vehicle: doc.vehicle, nodes, edges };
}

export function serializeFlow(flow) {
    return JSON.stringify(flow, null, 2) + '\n';
}

export function fileBaseName(flow) {
    return flow.name.trim().replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'flow';
}

// --- connection rules --------------------------------------------------------

export function acceptsAll(accepts, types) {
    return accepts.includes('*') || types.every((t) => t === UNKNOWN_TYPE || accepts.includes(t));
}

// What a filter forwards: unknown, since engine plugin files don't declare
// what a filter replaces its input with (an H.264 encoder takes RawFrames and
// forwards EncodedFrames), so whatever follows a filter isn't type-checked.
export const UNKNOWN_TYPE = '*';

function isFilter(flow, catalog, node) {
    return node?.kind === 'engine' && resolveNode(flow, catalog, node).category === 'filter';
}

// The source types reaching an engine: its producer inputs' stream types plus
// whatever its engine parent forwards. Engines forward the source data they
// receive; their results travel as events, not along output edges.
export function forwardedTypes(flow, catalog, nodeId, seen = new Set()) {
    if (seen.has(nodeId)) return [];
    seen.add(nodeId);
    if (isFilter(flow, catalog, nodeById(flow, nodeId))) return [UNKNOWN_TYPE];
    const types = [];
    for (const edge of flow.edges) {
        if (edge.kind !== 'output' || edge.target !== nodeId) continue;
        const source = nodeById(flow, edge.source);
        if (!source) continue;
        const arriving = source.kind === 'producer'
            ? [resolveNode(flow, catalog, source)].filter((r) => r.resolved).map((r) => r.output)
            : forwardedTypes(flow, catalog, source.id, seen);
        for (const t of arriving) if (!types.includes(t)) types.push(t);
    }
    return types;
}

// What an output edge from `source` carries.
function arrivingTypes(flow, catalog, source) {
    if (source.kind === 'engine') return forwardedTypes(flow, catalog, source.id);
    const r = resolveNode(flow, catalog, source);
    return r.resolved ? [r.output] : [];
}

// The engine feeding `nodeId` through an output edge, if any. Each engine
// has at most one, so following these links walks a single chain upstream.
function engineParent(flow, nodeId) {
    const edge = flow.edges.find((e) => e.kind === 'output' && e.target === nodeId
        && nodeById(flow, e.source)?.kind === 'engine');
    return edge ? edge.source : null;
}

// The first engine-to-engine edge at or below `startId` in `flow` that would
// carry a type its target doesn't accept *and* that `before` (the flow
// without the new edge) didn't already forward there -- an existing problem
// in a stale flow is validateFlow's to report, not the new edge's fault.
function downstreamProblem(flow, before, catalog, startId) {
    const seen = new Set();
    const queue = [startId];
    while (queue.length) {
        const id = queue.shift();
        if (seen.has(id)) continue;
        seen.add(id);
        for (const edge of flow.edges) {
            if (edge.kind !== 'output' || edge.source !== id) continue;
            const target = nodeById(flow, edge.target);
            if (target?.kind !== 'engine') continue;
            const to = resolveNode(flow, catalog, target);
            if (to.resolved) {
                const already = forwardedTypes(before, catalog, id);
                const missing = forwardedTypes(flow, catalog, id)
                    .filter((t) => !acceptsAll(to.accepts, [t]) && !already.includes(t));
                if (missing.length) {
                    return `this would forward ${missing.map(shortType).join(', ')} to ${nodeLabel(flow, target)}, which does not accept it`;
                }
            }
            queue.push(target.id);
        }
    }
    return null;
}

// `checkDownstream` is for new connections only: validateFlow already checks
// every existing edge against the whole flow's forwarded sets.
function outputProblem(flow, catalog, source, target, checkDownstream = false) {
    if (target.kind !== 'engine') return 'output edges must end at an engine';
    if (source.id === target.id) return 'an engine cannot feed itself';
    const from = resolveNode(flow, catalog, source);
    const to = resolveNode(flow, catalog, target);
    if (!from.resolved || !to.resolved) return 'cannot connect an unresolved node';
    const missing = arrivingTypes(flow, catalog, source).filter((t) => !acceptsAll(to.accepts, [t]));
    if (missing.length) return `${nodeLabel(flow, target)} does not accept ${missing.map(shortType).join(', ')}`;
    if (flow.edges.some((e) => e.kind === 'output' && e.source === source.id && e.target === target.id)) {
        return 'these nodes are already connected';
    }
    if (source.kind === 'engine') {
        if (engineParent(flow, target.id)) return `${nodeLabel(flow, target)} already has an engine input`;
        const seen = new Set();
        for (let cur = source.id; cur && !seen.has(cur); cur = engineParent(flow, cur)) {
            if (cur === target.id) return 'this would create a cycle';
            seen.add(cur);
        }
    }
    if (checkDownstream) {
        const tentative = { ...flow, edges: [...flow.edges, { id: '', kind: 'output', source: source.id, target: target.id }] };
        return downstreamProblem(tentative, flow, catalog, target.id);
    }
    return null;
}

function callbackEvents(flow, sourceId, targetId) {
    return new Set(flow.edges
        .filter((e) => e.kind === 'callback' && e.source === sourceId && e.target === targetId)
        .map((e) => e.event));
}

// `event` null means "is there any event left to connect with".
function callbackProblem(flow, catalog, source, target, event) {
    if (source.kind !== 'engine') return 'callbacks must start at an engine';
    if (source.id === target.id) return 'a callback cannot target its own engine';
    const from = resolveNode(flow, catalog, source);
    const to = resolveNode(flow, catalog, target);
    if (!from.resolved || !to.resolved) return 'cannot connect an unresolved node';
    const label = nodeLabel(flow, source);
    if (from.events.length === 0) return `${label} declares no events`;
    const used = callbackEvents(flow, source.id, target.id);
    if (event === null) {
        return from.events.every((ev) => used.has(ev))
            ? `every event of ${label} already has a callback to ${nodeLabel(flow, target)}`
            : null;
    }
    if (!from.events.includes(event)) return `${label} does not emit ${event}`;
    if (used.has(event)) return `a ${event} callback to ${nodeLabel(flow, target)} already exists`;
    return null;
}

export function canConnect(flow, catalog, sourceId, targetId, kind) {
    const source = nodeById(flow, sourceId);
    const target = nodeById(flow, targetId);
    if (!source || !target) return 'unknown node';
    return kind === 'callback'
        ? callbackProblem(flow, catalog, source, target, null)
        : outputProblem(flow, catalog, source, target, true);
}

export function addEdge(flow, catalog, sourceId, targetId, kind) {
    const problem = canConnect(flow, catalog, sourceId, targetId, kind);
    if (problem) throw new Error(problem);
    const base = { id: nextId(flow.edges, 'x'), kind, source: sourceId, target: targetId };
    let edge;
    if (kind === 'callback') {
        const { events } = resolveNode(flow, catalog, nodeById(flow, sourceId));
        const used = callbackEvents(flow, sourceId, targetId);
        edge = { ...base, event: events.find((ev) => !used.has(ev)) };
    } else {
        edge = { ...base, timeout: DEFAULT_TIMEOUT };
    }
    return { ...flow, edges: [...flow.edges, edge] };
}

// --- validation ----------------------------------------------------------------

const TIMEOUT_PATTERN = /^\d+(\.\d+)?(ms|s|m)$/;

// What makes two nodes the same one: its resolved daemon and bare name, so
// "swap03.yolo" and "100.64.0.2.yolo" count as one engine.
function nodeKey(catalog, node) {
    if (node.kind === 'producer') return `p:${node.stream}`;
    return `e:${serviceName(catalog, node.engine).name ?? node.engine}`;
}

// Why an existing edge is invalid, checked as if it were being added to the
// flow without it, so it isn't refused for duplicating itself.
function edgeProblem(flow, catalog, edge) {
    const others = { ...flow, edges: flow.edges.filter((e) => e.id !== edge.id) };
    const source = nodeById(flow, edge.source);
    const target = nodeById(flow, edge.target);
    if (edge.kind === 'callback') return callbackProblem(others, catalog, source, target, edge.event);
    return outputProblem(others, catalog, source, target)
        ?? (TIMEOUT_PATTERN.test(edge.timeout) ? null : `timeout "${edge.timeout}" is not a duration like 500ms, 1s, or 2m`);
}

export function validateFlow(flow, catalog) {
    const errors = [];
    const warnings = [];
    if (!flow.vehicle) errors.push({ message: 'No vehicle selected' });
    // The vehicle's plugin-file problem (a dropped camera, no plugin file),
    // once: its producers' own warnings don't repeat it.
    const vehicleProblem = catalog && flow.vehicle && findVehicle(catalog, flow.vehicle)?.problem;
    if (vehicleProblem) warnings.push({ message: `${displayName(flow.vehicle)}: ${vehicleProblem}` });

    const resolved = new Set();
    const seen = new Set();
    for (const node of flow.nodes) {
        const label = nodeLabel(flow, node);
        const key = nodeKey(catalog, node);
        if (seen.has(key)) errors.push({ nodeId: node.id, message: `${label} appears more than once` });
        seen.add(key);
        if (node.kind === 'producer' && !(Number.isInteger(node.tokens) && node.tokens >= 1)) {
            errors.push({ nodeId: node.id, message: `${label}: tokens must be a whole number of at least 1` });
        }
        const resolution = resolveNode(flow, catalog, node);
        if (!resolution.resolved) {
            errors.push({ nodeId: node.id, message: `${label}: ${resolution.reason}` });
            continue;
        }
        resolved.add(node.id);
        if (node.kind === 'engine' && resolution.location === null) {
            errors.push({ nodeId: node.id, message: `${label}: neither on ${flow.vehicle ? `${displayName(flow.vehicle)}'s daemon` : "the vehicle's daemon"} nor on the orchestrator` });
        }
        const engineProblem = node.kind === 'engine' ? resolution.problem : null;
        if (!resolution.typed) {
            const why = engineProblem ? ` (${engineProblem})` : '';
            warnings.push({ nodeId: node.id, message: `${label}: no type information${why}; connections are not type-checked` });
        } else if (engineProblem) {
            warnings.push({ nodeId: node.id, message: `${label}: ${engineProblem}` });
        }
        if (node.kind === 'engine' && !flow.edges.some((e) => e.kind === 'output' && e.target === node.id)) {
            warnings.push({ nodeId: node.id, message: `Nothing feeds into ${label}` });
        }
    }

    const primaries = flow.nodes.filter((n) => n.kind === 'producer' && resolved.has(n.id) && resolveNode(flow, catalog, n).stream?.primary);
    for (const node of primaries.slice(1)) {
        errors.push({
            nodeId: node.id,
            message: `${nodeLabel(flow, node)} and ${nodeLabel(flow, primaries[0])} are both primary cameras; only one can stream at a time`,
        });
    }

    for (const edge of flow.edges) {
        if (!resolved.has(edge.source) || !resolved.has(edge.target)) continue;
        if (edge.kind === 'output' && isFilter(flow, catalog, nodeById(flow, edge.source))) {
            warnings.push({
                edgeId: edge.id,
                message: `${nodeLabel(flow, nodeById(flow, edge.source))} → ${nodeLabel(flow, nodeById(flow, edge.target))}: ${nodeLabel(flow, nodeById(flow, edge.source))} is a filter, so what it forwards is not type-checked`,
            });
        }
        const problem = edgeProblem(flow, catalog, edge);
        if (problem) {
            const where = `${nodeLabel(flow, nodeById(flow, edge.source))} → ${nodeLabel(flow, nodeById(flow, edge.target))}`;
            errors.push({ edgeId: edge.id, message: `${where}: ${problem}` });
        }
    }

    if (flow.vehicle) {
        const byName = new Map();
        for (const node of flow.nodes) {
            const name = exportName(flow, catalog, node);
            byName.set(name, [...(byName.get(name) ?? []), node]);
        }
        for (const [name, nodes] of byName) {
            if (nodes.length > 1 && new Set(nodes.map((n) => nodeKey(catalog, n))).size > 1) {
                errors.push({
                    nodeId: nodes[1].id,
                    message: `${nodes.map((n) => nodeLabel(flow, n)).join(' and ')} would both be named "${name}" in the flow`,
                });
            }
        }
    }
    return { errors, warnings };
}

// --- Gabriel TOML export -----------------------------------------------------------

// JSON string escaping is valid TOML basic-string escaping.
const tomlString = (s) => JSON.stringify(s);

// The flow as the template the GCS expects at Deploy: every producer and
// engine by its template name, and output edges only (callbacks are a
// drawing aid). Where an engine runs is the GCS's to work out per target.
export function toTemplate(flow, catalog) {
    const byId = new Map(flow.nodes.map((n) => [n.id, n]));
    const name = (node) => exportName(flow, catalog, node);
    const engines = flow.nodes.filter((n) => n.kind === 'engine').map((n) => {
        if (engineLocation(catalog, flow.vehicle, n.engine) === null) {
            throw new Error(`${nodeLabel(flow, n)} is neither on the vehicle's daemon nor on the orchestrator`);
        }
        return { name: name(n) };
    });
    return {
        vehicle: flow.vehicle,
        producers: flow.nodes.filter((n) => n.kind === 'producer').map((n) => ({ name: name(n), tokens: n.tokens })),
        engines,
        edges: flow.edges.filter((e) => e.kind === 'output').map((e) => ({
            from: name(byId.get(e.source)),
            to: name(byId.get(e.target)),
            timeout: e.timeout,
        })),
    };
}

// The flow as a config.FlowConfig TOML document: top-level [[producers]],
// [[engines]] and [[edges]] tables. Engines carry their location relative to
// the flow's vehicle. Callbacks have no TOML representation, so they're left
// out (and counted in a comment).
export function toFlowToml(flow, catalog) {
    const { errors } = validateFlow(flow, catalog);
    if (errors.length) throw new Error(`flow has ${errors.length} error(s); fix them before exporting TOML`);

    const template = toTemplate(flow, catalog);
    const producers = template.producers.flatMap((p) => [
        '[[producers]]',
        `name = ${tomlString(p.name)}`,
        ...(p.tokens !== DEFAULT_TOKENS ? [`tokens = ${p.tokens}`] : []),
    ]);
    const engines = template.engines.flatMap((e) => [
        '[[engines]]',
        `name = ${tomlString(e.name)}`,
        `location = ${tomlString(engineLocation(catalog, flow.vehicle, e.name))}`,
    ]);
    const edges = template.edges.flatMap((e) => [
        '[[edges]]',
        `from = ${tomlString(e.from)}`,
        `to = ${tomlString(e.to)}`,
        `timeout = ${tomlString(e.timeout)}`,
    ]);

    const callbacks = flow.edges.filter((e) => e.kind === 'callback').length;
    const header = callbacks
        ? `# ${callbacks} callback edge(s) omitted: flow TOML has no callback representation.\n\n`
        : '';
    const sections = [producers, engines, edges].filter((lines) => lines.length).map((lines) => lines.join('\n'));
    return header + sections.join('\n\n') + '\n';
}

// --- TOML import ---------------------------------------------------------------

const COLUMN_WIDTH = 250;
const ROW_HEIGHT = 150;

// The inverse of toTemplate for `vehicle`. Producers become the vehicle's
// streams of their names, whatever vehicle their prefix names. An engine name
// with no host (an old TOML export, where bare meant the vehicle's own daemon)
// is qualified with that daemon's name. Engine hosts and engines the catalog doesn't know are kept, so
// validateFlow flags them. Nodes are laid out in columns: producers, then
// engines by depth.
export function fromTemplate(template, catalog, vehicle) {
    const daemon = vehicle ? findDaemon(catalog, daemonOf(vehicle)) : null;
    const home = daemon ? hostLabel(catalog, daemon) : null;
    // The server sends empty lists as JSON null.
    const tProducers = template.producers ?? [];
    const tEngines = template.engines ?? [];
    const tEdges = template.edges ?? [];
    const producers = tProducers.map((p, i) => {
        const parts = splitTemplateName(p.name);
        return {
            id: `p${i + 1}`, kind: 'producer', stream: parts?.bare ?? p.name,
            tokens: p.tokens ?? DEFAULT_TOKENS, position: { x: 0, y: 0 },
        };
    });
    const engines = tEngines.map((e, i) => ({
        id: `e${i + 1}`, kind: 'engine',
        engine: home && !splitTemplateName(e.name) ? `${home}.${e.name}` : e.name, position: { x: 0, y: 0 },
    }));
    const ids = new Map();
    for (const [i, p] of tProducers.entries()) if (!ids.has(p.name)) ids.set(p.name, producers[i].id);
    for (const [i, e] of tEngines.entries()) if (!ids.has(e.name)) ids.set(e.name, engines[i].id);

    const edges = tEdges.map((e, i) => {
        const source = ids.get(e.from);
        const target = ids.get(e.to);
        if (!source || !target) throw new Error(`edge ${e.from} -> ${e.to} names a node that is not in the flow`);
        return { id: `x${i + 1}`, kind: 'output', source, target, timeout: e.timeout };
    });

    // Depth: producers 0, an engine one past its deepest source (bounded so
    // a cyclic file still lays out).
    const depth = new Map(producers.map((p) => [p.id, 0]));
    for (const e of engines) depth.set(e.id, 1);
    for (let pass = 0; pass < engines.length; pass++) {
        for (const edge of edges) {
            if (depth.get(edge.target) < depth.get(edge.source) + 1 && depth.get(edge.target) > 0) {
                depth.set(edge.target, depth.get(edge.source) + 1);
            }
        }
    }
    const rows = new Map();
    const nodes = [...producers, ...engines].map((n) => {
        const col = depth.get(n.id);
        const row = rows.get(col) ?? 0;
        rows.set(col, row + 1);
        return { ...n, position: { x: col * COLUMN_WIDTH, y: row * ROW_HEIGHT } };
    });
    return { ...emptyFlow(), vehicle, nodes, edges };
}

// Parses flow TOML text on the server and builds a flow for `vehicle`.
// Throws the server's reason (unknown keys, the old format, parse errors).
export async function importFlowToml(text, catalog, vehicle) {
    const result = await daemonRequest('POST', '/api/flows/from_toml', { toml: text });
    const failure = describeFailure(result);
    if (failure) throw new Error(failure);
    return fromTemplate(result.body, catalog, vehicle);
}
