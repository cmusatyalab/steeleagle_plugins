import { describe, it, expect, afterEach, vi } from 'vitest';
import {
    addEngine, addProducer, availableStreams, emptyFlow, exportName, fileBaseName, isEnginePlaced,
    nodeLabel, parseFlowJson, removeEdge, removeNode, serializeFlow, setVehicle, updateEdge, updateNode,
    acceptsAll, addEdge, canConnect, forwardedTypes, streamChoices, toFlowToml, toTemplate, validateFlow,
    fromTemplate, importFlowToml,
} from './flowModel.js';
import { GCS, HARPY, HEISENBERG, LAB, SWAP, TYPES, catalogFixture, flowB } from './flowFixtures.js';
import { findDaemon, hostLabel } from './flowCatalog.js';
import { daemonOf } from './vehicleNames.js';

const AT = { x: 1, y: 2 };
const KESTREL = `${SWAP.ip}.kestrel`;
const yolo = (daemon) => `${daemon.ip}.yolo`;
// The catalog with `fn` applied to every daemon.
const mapDaemons = (cat, fn) => ({ ...cat, daemons: cat.daemons.map(fn) });
// The catalog with `fn` applied to the engines named `bare` (on every daemon).
const mapEngines = (cat, bare, fn) => mapDaemons(cat, (d) => ({
    ...d, engines: d.engines.map((e) => (e.name === `${d.ip}.${bare}` ? fn(e) : e)),
}));

describe('document operations', () => {
    it('starts empty with no vehicle', () => {
        expect(emptyFlow()).toEqual({ schemaVersion: 3, name: 'untitled-flow', vehicle: null, nodes: [], edges: [] });
    });

    // The catalog with `daemon`'s hostname removed, so flows name it by address.
    const withoutHostname = (cat, daemon) => mapDaemons(cat, (d) => (d.ip === daemon.ip ? { ...d, hostname: '' } : d));

    it('adds producers with no host, named for the vehicle, with default tokens, refusing duplicates or no vehicle', () => {
        const catalog = catalogFixture();
        const flow = setVehicle(emptyFlow(), HARPY);
        const next = addProducer(flow, catalog, 'frames', AT);
        expect(next.nodes).toEqual([{ id: 'p1', kind: 'producer', stream: 'frames', tokens: 1, position: AT }]);
        expect(nodeLabel(next, next.nodes[0])).toBe('harpy.frames');
        expect(() => addProducer(next, catalog, 'frames', AT)).toThrow('harpy.frames is already on the canvas');
        expect(() => addProducer(emptyFlow(), catalog, 'frames', AT)).toThrow('Select a vehicle first');
    });

    it('names a producer for the vehicle even when its daemon has no hostname', () => {
        const catalog = withoutHostname(catalogFixture(), SWAP);
        const next = addProducer(setVehicle(emptyFlow(), HARPY), catalog, 'frames', AT);
        expect(next.nodes[0]).not.toHaveProperty('host');
        expect(nodeLabel(next, next.nodes[0])).toBe('harpy.frames');
    });

    it('adds engines by template name, each at most once', () => {
        const catalog = catalogFixture();
        const base = setVehicle(emptyFlow(), HARPY);
        const once = addEngine(base, catalog, yolo(SWAP), AT);
        expect(once.nodes[0]).toEqual({ id: 'e1', kind: 'engine', engine: 'swap.yolo', position: AT });
        expect(() => addEngine(once, catalog, yolo(SWAP), AT)).toThrow('swap.yolo is already on the canvas');
        // the same engine name on the orchestrator is a different engine
        const both = addEngine(once, catalog, yolo(GCS), AT);
        expect(both.nodes.map((n) => n.engine)).toEqual(['swap.yolo', 'gcs.yolo']);
        expect(isEnginePlaced(once, catalog, yolo(SWAP))).toBe(true);
        expect(isEnginePlaced(once, catalog, yolo(GCS))).toBe(false);
    });

    it('stores an address for an engine on a daemon with no hostname', () => {
        const catalog = withoutHostname(catalogFixture(), GCS);
        const once = addEngine(setVehicle(emptyFlow(), HARPY), catalog, yolo(GCS), AT);
        expect(once.nodes[0].engine).toBe('100.64.0.1.yolo');
        expect(isEnginePlaced(once, catalog, yolo(GCS))).toBe(true);
    });

    it('sees an engine as placed however its host is spelled', () => {
        const catalog = catalogFixture();
        const flow = addEngine(setVehicle(emptyFlow(), HARPY), catalog, yolo(SWAP), AT);
        const byIp = { ...flow, nodes: [{ ...flow.nodes[0], engine: '100.64.0.2.yolo' }] };
        expect(isEnginePlaced(byIp, catalog, yolo(SWAP))).toBe(true);
        expect(() => addEngine(byIp, catalog, yolo(SWAP), AT)).toThrow('swap.yolo is already on the canvas');
    });

    it('rejects an engine on a third daemon, or any engine without a vehicle', () => {
        const catalog = catalogFixture();
        const base = setVehicle(emptyFlow(), HARPY);
        expect(() => addEngine(base, catalog, yolo(LAB), AT))
            .toThrow("lab.yolo is neither on harpy's daemon nor on the orchestrator");
        expect(() => addEngine(emptyFlow(), catalog, yolo(GCS), AT)).toThrow('Select a vehicle first');
    });

    it('allocates ids past the highest existing one', () => {
        const flow = addEngine(flowB(), catalogFixture(), yolo(GCS), AT);
        expect(flow.nodes.at(-1).id).toBe('e3');
    });

    it('lists the bound vehicle streams not yet placed', () => {
        const flow = { ...flowB(), nodes: flowB().nodes.filter((n) => n.stream !== 'frames') };
        expect(availableStreams(flow, catalogFixture())).toEqual(['frames', 'thermal', 'downward']);
        expect(availableStreams(flowB(), catalogFixture())).toEqual(['downward']);
        expect(availableStreams(emptyFlow(), catalogFixture())).toEqual([]);
    });

    it('updates and removes nodes and edges', () => {
        const flow = flowB();
        expect(updateNode(flow, 'p1', { tokens: 3 }).nodes[0].tokens).toBe(3);
        expect(updateEdge(flow, 'x1', { timeout: '5s' }).edges[0].timeout).toBe('5s');
        const withoutYolo = removeNode(flow, 'e1');
        expect(withoutYolo.nodes.map((n) => n.id)).toEqual(['p1', 'p2', 'e2']);
        expect(withoutYolo.edges.map((e) => e.id)).toEqual(['x1']);
        expect(removeEdge(flow, 'x2').edges.map((e) => e.id)).toEqual(['x1', 'x3']);
    });

    it('drops producers and their edges when the vehicle changes, keeping engines', () => {
        const flow = setVehicle(flowB(), KESTREL);
        expect(flow.vehicle).toBe(KESTREL);
        expect(flow.nodes.map((n) => n.id)).toEqual(['e1', 'e2']);
        expect(flow.edges.map((e) => e.id)).toEqual(['x3']);
    });

    it('keeps producers when the same vehicle is set again', () => {
        const flow = setVehicle(flowB(), HARPY);
        expect(flow.nodes).toHaveLength(4);
    });

    it('labels nodes and names them for the template', () => {
        const flow = flowB();
        expect(nodeLabel(flow, flow.nodes[1])).toBe('harpy.frames');
        expect(nodeLabel(flow, flow.nodes[2])).toBe('100.64.0.2.yolo');
        expect(flow.nodes.map((n) => exportName(flow, catalogFixture(), n)))
            .toEqual(['harpy.telemetry', 'harpy.frames', '100.64.0.2.yolo', '100.64.0.1.telemetry']);
    });

    it('derives a safe export file name', () => {
        expect(fileBaseName(flowB())).toBe('early-discard');
        expect(fileBaseName({ ...flowB(), name: 'Early discard / v2' })).toBe('Early-discard-v2');
        expect(fileBaseName({ ...flowB(), name: '  ' })).toBe('flow');
    });
});

describe('JSON import/export', () => {
    it('round-trips a flow', () => {
        expect(parseFlowJson(serializeFlow(flowB()))).toEqual(flowB());
    });

    it('loads a v3 file whose producers still have a host, dropping it', () => {
        const doc = flowB();
        doc.nodes[0].host = 'swap';
        const back = parseFlowJson(JSON.stringify(doc));
        expect(back.nodes[0]).not.toHaveProperty('host');
        expect(back).toEqual(flowB());
    });

    it('fills in missing tokens and timeouts with defaults', () => {
        const doc = flowB();
        delete doc.nodes[0].tokens;
        delete doc.edges[0].timeout;
        const flow = parseFlowJson(JSON.stringify(doc));
        expect(flow.nodes[0].tokens).toBe(1);
        expect(flow.edges[0].timeout).toBe('1s');
    });

    it.each([
        ['{not json', /not valid JSON/],
        ['[]', 'not a flow document'],
        [JSON.stringify({ ...flowB(), schemaVersion: 2 }), "This flow was saved by an older Flow Builder and can't be loaded; rebuild it or load its TOML export"],
        [JSON.stringify({ ...flowB(), schemaVersion: 4 }), 'unsupported schemaVersion 4 (expected 3)'],
        [JSON.stringify({ ...flowB(), name: 7 }), 'name must be a string'],
        [JSON.stringify({ ...flowB(), vehicle: { name: 'harpy' } }), 'vehicle must be null or a full vehicle name'],
        [JSON.stringify({ ...flowB(), nodes: {} }), 'nodes and edges must be arrays'],
        [JSON.stringify({ ...flowB(), nodes: [...flowB().nodes, flowB().nodes[0]] }), 'duplicate node id p1'],
        [JSON.stringify({ ...flowB(), nodes: [{ ...flowB().nodes[0], kind: 'sink' }], edges: [] }), 'node p1: unknown kind "sink"'],
        [JSON.stringify({ ...flowB(), nodes: [{ ...flowB().nodes[2], engine: 7 }], edges: [] }), 'node e1: engine nodes need an engine template name'],
        [JSON.stringify({ ...flowB(), nodes: [{ ...flowB().nodes[0], position: null }], edges: [] }), 'node p1: position must be {x, y}'],
        [JSON.stringify({ ...flowB(), edges: [{ ...flowB().edges[0], target: 'zz' }] }), 'edge x1 references a missing node'],
        [JSON.stringify({ ...flowB(), edges: [...flowB().edges, flowB().edges[0]] }), 'duplicate edge id x1'],
        [JSON.stringify({ ...flowB(), edges: [{ id: 'x9', kind: 'callback', source: 'e1', target: 'p2' }] }), 'edge x9: callback edges need an event'],
        [JSON.stringify({ ...flowB(), edges: [{ id: 'x9', kind: 'magic', source: 'e1', target: 'p2' }] }), 'edge x9: unknown kind "magic"'],
    ])('rejects %s', (text, message) => {
        expect(() => parseFlowJson(text)).toThrow(message);
    });
});

describe('acceptsAll', () => {
    it('requires every type to be accepted, with * accepting anything', () => {
        expect(acceptsAll(['image'], ['image'])).toBe(true);
        expect(acceptsAll(['image'], ['image', 'telemetry'])).toBe(false);
        expect(acceptsAll(['telemetry', 'image'], ['image', 'telemetry'])).toBe(true);
        expect(acceptsAll(['*'], ['telemetry'])).toBe(true);
        expect(acceptsAll(['image'], [])).toBe(true);
    });
});

describe('forwardedTypes', () => {
    const catalog = catalogFixture();

    it('collects producer inputs and the engine parent, in order', () => {
        expect(forwardedTypes(flowB(), catalog, 'e1')).toEqual([TYPES.raw]);
        expect(forwardedTypes(flowB(), catalog, 'e2')).toEqual([TYPES.telemetry, TYPES.raw]);
    });

    it('is empty for an engine with no inputs', () => {
        expect(forwardedTypes({ ...flowB(), edges: [] }, catalog, 'e1')).toEqual([]);
    });

    it('terminates on a cycle in an imported flow', () => {
        const flow = { ...flowB(), edges: [...flowB().edges, { id: 'x9', kind: 'output', source: 'e2', target: 'e1', timeout: '1s' }] };
        expect(forwardedTypes(flow, catalog, 'e1')).toEqual([TYPES.raw, TYPES.telemetry]);
    });
});

describe('output edges', () => {
    const catalog = catalogFixture();
    // Flow B without edges, plus detector on the vehicle's daemon as a second engine.
    const bare = () => addEngine({ ...flowB(), edges: [] }, catalog, `${SWAP.ip}.detector`, AT); // detector = e3

    it('builds Flow B edge by edge', () => {
        let flow = bare();
        flow = addEdge(flow, catalog, 'p1', 'e2', 'output');
        flow = addEdge(flow, catalog, 'p2', 'e1', 'output');
        flow = addEdge(flow, catalog, 'e1', 'e2', 'output');
        expect(flow.edges).toEqual([
            { id: 'x1', kind: 'output', source: 'p1', target: 'e2', timeout: '1s' },
            { id: 'x2', kind: 'output', source: 'p2', target: 'e1', timeout: '1s' },
            { id: 'x3', kind: 'output', source: 'e1', target: 'e2', timeout: '1s' },
        ]);
    });

    it('only ends at engines', () => {
        expect(canConnect(bare(), catalog, 'e1', 'p1', 'output')).toBe('output edges must end at an engine');
    });

    it('checks types', () => {
        expect(canConnect(bare(), catalog, 'p1', 'e1', 'output')).toBe('100.64.0.2.yolo does not accept Telemetry');
    });

    it('allows many producer inputs but only one engine input', () => {
        const flow = addEdge(addEdge(bare(), catalog, 'p1', 'e2', 'output'), catalog, 'e1', 'e2', 'output');
        // detector forwards nothing yet, so only the engine-input rule refuses
        expect(canConnect(flow, catalog, 'e3', 'e2', 'output')).toBe('100.64.0.1.telemetry already has an engine input');
        // a second producer input is still fine
        const twoCams = addEdge(bare(), catalog, 'p2', 'e1', 'output');
        expect(canConnect(twoCams, catalog, 'p2', 'e3', 'output')).toBeNull();
    });

    it('refuses duplicates, self-loops, and cycles', () => {
        const flow = addEdge(bare(), catalog, 'p2', 'e1', 'output');
        expect(canConnect(flow, catalog, 'p2', 'e1', 'output')).toBe('these nodes are already connected');
        expect(canConnect(flow, catalog, 'e1', 'e1', 'output')).toBe('an engine cannot feed itself');
        // e1 -> e3 is fine; then e3 -> e1 would close a loop. Use wildcard
        // engines so types don't get in the way.
        const wild = mapDaemons(catalog, (d) => ({ ...d, engines: d.engines.map((e) => ({ ...e, accepts: ['*'] })) }));
        const chain = addEdge(bare(), wild, 'e1', 'e3', 'output');
        expect(canConnect(chain, wild, 'e3', 'e1', 'output')).toBe('this would create a cycle');
    });

    it('refuses unresolved nodes', () => {
        const flow = addEngine(bare(), catalog, `${SWAP.ip}.tracker`, AT); // e4, not in the catalog
        expect(canConnect(flow, catalog, 'p2', 'e4', 'output')).toBe('cannot connect an unresolved node');
    });

    it('throws from addEdge when refused', () => {
        expect(() => addEdge(bare(), catalog, 'p1', 'e1', 'output')).toThrow('does not accept Telemetry');
    });

    it('refuses an engine edge unless every forwarded type is accepted', () => {
        // telemetry (e2) takes both streams, then forwards telemetry + image;
        // detector (e3) accepts image only.
        let flow = addEdge(bare(), catalog, 'p1', 'e2', 'output');
        flow = addEdge(flow, catalog, 'p2', 'e2', 'output');
        expect(canConnect(flow, catalog, 'e2', 'e3', 'output')).toBe('swap.detector does not accept Telemetry');
        // yolo forwards image only, which detector accepts
        const yoloChain = addEdge(bare(), catalog, 'p2', 'e1', 'output');
        expect(canConnect(yoloChain, catalog, 'e1', 'e3', 'output')).toBeNull();
    });

    it('lets an engine with no inputs connect onward (nothing forwarded yet)', () => {
        expect(canConnect(bare(), catalog, 'e2', 'e3', 'output')).toBeNull();
    });

    it('downstream re-check: refuses an input that would push an unaccepted type down the chain', () => {
        let flow = addEdge(bare(), catalog, 'p2', 'e2', 'output'); // e2 forwards image
        flow = addEdge(flow, catalog, 'e2', 'e3', 'output');       // detector accepts image
        expect(canConnect(flow, catalog, 'p1', 'e2', 'output'))
            .toBe('this would forward Telemetry to swap.detector, which does not accept it');
        expect(() => addEdge(flow, catalog, 'p1', 'e2', 'output')).toThrow('which does not accept it');
    });

    it('downstream re-check only blames types the new edge introduces', () => {
        // Stale/imported: e2 already forwards telemetry to image-only detector.
        const stale = { ...bare(), edges: [
            { id: 'x1', kind: 'output', source: 'p1', target: 'e2', timeout: '1s' },
            { id: 'x2', kind: 'output', source: 'e2', target: 'e3', timeout: '1s' },
        ] };
        // Adding frames only adds image, which detector accepts: allowed
        // (the existing x2 problem stays flagged by validateFlow).
        expect(canConnect(stale, catalog, 'p2', 'e2', 'output')).toBeNull();
    });
});

describe('callback edges', () => {
    const catalog = catalogFixture();

    it('picks the first event not yet used toward that target', () => {
        let flow = addEdge(flowB(), catalog, 'e1', 'p2', 'callback');
        flow = addEdge(flow, catalog, 'e1', 'p2', 'callback');
        expect(flow.edges.slice(-2)).toEqual([
            { id: 'x4', kind: 'callback', source: 'e1', target: 'p2', event: 'result' },
            { id: 'x5', kind: 'callback', source: 'e1', target: 'p2', event: 'dropped' },
        ]);
        expect(canConnect(flow, catalog, 'e1', 'p2', 'callback')).toBe('every event of 100.64.0.2.yolo already has a callback to harpy.frames');
    });

    it('may target any other node', () => {
        expect(canConnect(flowB(), catalog, 'e1', 'e2', 'callback')).toBeNull();
        expect(canConnect(flowB(), catalog, 'e1', 'p1', 'callback')).toBeNull();
    });

    it('must start at an engine that declares events', () => {
        expect(canConnect(flowB(), catalog, 'p2', 'e1', 'callback')).toBe('callbacks must start at an engine');
        // Every plugin-file engine emits result; an odd catalog entry may not.
        const silent = mapEngines(catalog, 'telemetry', (e) => ({ ...e, events: [] }));
        expect(canConnect(flowB(), silent, 'e2', 'p1', 'callback')).toBe('100.64.0.1.telemetry declares no events');
        expect(canConnect(flowB(), catalog, 'e1', 'e1', 'callback')).toBe('a callback cannot target its own engine');
    });
});

describe('primary cameras', () => {
    const catalog = catalogFixture();
    const harpy = () => ({ ...emptyFlow(), vehicle: HARPY });
    const choice = (choices, name) => choices.find((c) => c.name === name);

    it('offers primary cameras until one is placed', () => {
        expect(availableStreams(harpy(), catalog)).toEqual(['telemetry', 'frames', 'thermal', 'downward']);
        expect(availableStreams(addProducer(harpy(), catalog, 'frames', AT), catalog)).toEqual(['telemetry', 'downward']);
    });

    it('marks the blocked primary camera with a reason', () => {
        const flow = addProducer(harpy(), catalog, 'frames', AT);
        expect(choice(streamChoices(flow, catalog), 'thermal')).toEqual({
            name: 'thermal', primary: true, disabled: true, reason: 'harpy.frames is already streaming a primary camera',
        });
        expect(choice(streamChoices(flow, catalog), 'downward')).toEqual({ name: 'downward', primary: false, disabled: false, reason: null });
    });

    it('lets the primary producer switch to another primary camera', () => {
        let flow = addProducer(harpy(), catalog, 'frames', AT);     // p1
        flow = addProducer(flow, catalog, 'telemetry', AT);         // p2
        expect(choice(streamChoices(flow, catalog, 'p1'), 'thermal')).toMatchObject({ disabled: false, reason: null });
        expect(choice(streamChoices(flow, catalog, 'p1'), 'frames')).toMatchObject({ primary: true, disabled: false });
        expect(choice(streamChoices(flow, catalog, 'p2'), 'thermal')).toMatchObject({ disabled: true });
    });

    it('keeps a producer\'s own stream even when the catalog lacks it', () => {
        const flow = addProducer(harpy(), catalog, 'lidar', AT);
        expect(choice(streamChoices(flow, catalog, 'p1'), 'lidar')).toEqual({ name: 'lidar', primary: false, disabled: false, reason: null });
    });

    it('reports two primary producers in an imported flow', () => {
        const flow = { ...harpy(), nodes: [
            { id: 'p1', kind: 'producer', stream: 'frames', tokens: 1, position: AT },
            { id: 'p2', kind: 'producer', stream: 'thermal', tokens: 1, position: AT },
        ] };
        const primaryErrors = validateFlow(flow, catalog).errors.filter((e) => e.message.includes('both primary cameras'));
        expect(primaryErrors).toEqual([
            { nodeId: 'p2', message: 'harpy.thermal and harpy.frames are both primary cameras; only one can stream at a time' },
        ]);
    });
});

describe('catalog problems', () => {
    const withProblems = (vehicleProblem, engineChange) => mapDaemons(catalogFixture(), (d) => ({
        ...d,
        vehicles: d.vehicles.map((v) => (v.name === HARPY ? { ...v, problem: vehicleProblem } : v)),
        engines: d.engines.map((e) => (e.name === yolo(SWAP) ? { ...e, ...engineChange } : e)),
    }));
    const messages = (flow, catalog) => validateFlow(flow, catalog).warnings.map((w) => w.message);

    it('warns about the bound vehicle\'s problem once, even with no producers', () => {
        const catalog = withProblems('camera "rgb" has no type; skipped', {});
        const flow = { ...emptyFlow(), vehicle: HARPY };
        expect(messages(flow, catalog)).toEqual(['harpy: camera "rgb" has no type; skipped']);
        expect(messages(flowB(), catalog).filter((m) => m.startsWith('harpy:'))).toHaveLength(1);
    });

    it('says why an engine is untyped', () => {
        const catalog = withProblems(null, { typed: false, problem: 'no plugin file', accepts: ['*'], events: [{ label: 'result', type: null }] });
        expect(messages(flowB(), catalog)).toContain('100.64.0.2.yolo: no type information (no plugin file); connections are not type-checked');
    });

    it('reports a typed engine\'s dropped entries', () => {
        const catalog = withProblems(null, { problem: 'an event has no label; skipped' });
        expect(messages(flowB(), catalog)).toContain('100.64.0.2.yolo: an event has no label; skipped');
    });
});

describe('result events', () => {
    const catalog = catalogFixture();

    it('defaults a new callback to result', () => {
        let flow = addEdge(flowB(), catalog, 'e1', 'p2', 'callback');
        expect(flow.edges.at(-1).event).toBe('result');
        flow = addEdge(flow, catalog, 'e1', 'p2', 'callback');
        expect(flow.edges.at(-1).event).toBe('dropped');
    });

    it('flags an old flow\'s detections callback', () => {
        const flow = { ...flowB(), edges: [...flowB().edges, { id: 'x4', kind: 'callback', source: 'e1', target: 'p2', event: 'detections' }] };
        expect(validateFlow(flow, catalog).errors.map((e) => e.message).join(' | ')).toContain('does not emit detections');
    });
});

describe('validateFlow', () => {
    const catalog = catalogFixture();
    const messages = (issues) => issues.map((i) => i.message);

    it('passes Flow B cleanly', () => {
        expect(validateFlow(flowB(), catalog)).toEqual({ errors: [], warnings: [] });
    });

    it('reports a local engine whose export name collides with a producer', () => {
        const flow = { ...flowB(), nodes: [...flowB().nodes, { id: 'e3', kind: 'engine', engine: 'harpy.frames', position: AT }] };
        expect(messages(validateFlow(flow, catalog).errors)).toContain(
            'harpy.frames and harpy.frames would both be named "harpy.frames" in the flow',
        );
    });

    it('catches a second engine input on an imported flow', () => {
        const base = addEngine(flowB(), catalog, `${SWAP.ip}.detector`, AT);
        const flow = { ...base, edges: [...base.edges, { id: 'x9', kind: 'output', source: 'e3', target: 'e2', timeout: '1s' }] };
        const { errors } = validateFlow(flow, catalog);
        expect(errors.some((e) => ['x9', 'x3'].includes(e.edgeId) && e.message.endsWith('already has an engine input'))).toBe(true);
    });

    it('requires a vehicle', () => {
        expect(messages(validateFlow(emptyFlow(), catalog).errors)).toEqual(['No vehicle selected']);
    });

    it('reports unresolved nodes against the node', () => {
        const down = {
            ...catalog,
            daemons: catalog.daemons.filter((d) => d.ip !== GCS.ip),
            unavailable: [{ ip: GCS.ip, hostname: 'gcs', reason: 'timeout' }],
        };
        const { errors } = validateFlow(flowB(), down);
        expect(errors).toContainEqual({ nodeId: 'e2', message: '100.64.0.1.telemetry: daemon gcs is unavailable: timeout' });
        // edges touching an unresolved node aren't double-reported
        expect(errors.filter((e) => e.edgeId)).toEqual([]);
    });

    it('reports an engine on a third daemon as unreachable', () => {
        const flow = {
            ...flowB(),
            nodes: [...flowB().nodes, { id: 'e3', kind: 'engine', engine: yolo(LAB), position: AT }],
        };
        expect(validateFlow(flow, catalog).errors).toContainEqual({
            nodeId: 'e3', message: "100.64.0.3.yolo: neither on harpy's daemon nor on the orchestrator",
        });
    });

    it('reports edges broken by a catalog change', () => {
        const changed = mapEngines(catalog, 'yolo', (e) => ({ ...e, accepts: ['lidar'] }));
        expect(validateFlow(flowB(), changed).errors).toEqual([
            { edgeId: 'x2', message: 'harpy.frames → 100.64.0.2.yolo: 100.64.0.2.yolo does not accept RawFrame' },
        ]);
    });

    it('reports a second engine input and a cycle from an imported file', () => {
        const wild = mapDaemons(catalog, (d) => ({ ...d, engines: d.engines.map((e) => ({ ...e, accepts: ['*'] })) }));
        const flow = {
            ...flowB(),
            edges: [
                ...flowB().edges,
                { id: 'x4', kind: 'output', source: 'e2', target: 'e1', timeout: '1s' },
            ],
        };
        const msgs = messages(validateFlow(flow, wild).errors);
        expect(msgs.some((m) => m.endsWith('this would create a cycle'))).toBe(true);
    });

    it('checks timeout format and tokens', () => {
        let flow = updateEdge(flowB(), 'x1', { timeout: '2 seconds' });
        flow = updateEdge(flow, 'x2', { timeout: '' });
        flow = updateNode(flow, 'p1', { tokens: 0 });
        expect(messages(validateFlow(flow, catalog).errors)).toEqual([
            'harpy.telemetry: tokens must be a whole number of at least 1',
            'harpy.telemetry → 100.64.0.1.telemetry: timeout "2 seconds" is not a duration like 500ms, 1s, or 2m',
            'harpy.frames → 100.64.0.2.yolo: timeout "" is not a duration like 500ms, 1s, or 2m',
        ]);
        for (const ok of ['500ms', '1s', '1.5s', '2m']) {
            expect(validateFlow(updateEdge(flowB(), 'x1', { timeout: ok }), catalog).errors).toEqual([]);
        }
    });

    it('checks callback events against the catalog', () => {
        const flow = { ...flowB(), edges: [...flowB().edges, { id: 'x4', kind: 'callback', source: 'e1', target: 'p2', event: 'exploded' }] };
        expect(messages(validateFlow(flow, catalog).errors)).toEqual([
            '100.64.0.2.yolo → harpy.frames: 100.64.0.2.yolo does not emit exploded',
        ]);
    });

    it('reports nodes placed twice by an imported file', () => {
        const flow = { ...flowB(), nodes: [...flowB().nodes, { ...flowB().nodes[2], id: 'e9' }] };
        expect(validateFlow(flow, catalog).errors).toContainEqual({ nodeId: 'e9', message: '100.64.0.2.yolo appears more than once' });
    });

    it('reports export-name collisions', () => {
        // a producer and an engine with the same template name
        const cassowary = `${GCS.ip}.cassowary`;
        const streams = [{ name: 'yolo', kind: 'telemetry', type: TYPES.telemetry, primary: false }];
        const cat = mapDaemons(catalog, (d) => (d.ip === GCS.ip ? { ...d, vehicles: [{ name: cassowary, streams, typed: true }] } : d));
        const flow = {
            ...emptyFlow(),
            vehicle: cassowary,
            nodes: [
                { id: 'p1', kind: 'producer', stream: 'yolo', tokens: 1, position: AT },
                { id: 'e1', kind: 'engine', engine: 'cassowary.yolo', position: AT },
            ],
        };
        expect(messages(validateFlow(flow, cat).errors)).toContain(
            'cassowary.yolo and cassowary.yolo would both be named "cassowary.yolo" in the flow',
        );
    });

    it('reports a forwarded type the downstream engine does not accept, once, on that edge', () => {
        const base = addEngine(flowB(), catalog, `${SWAP.ip}.detector`, AT); // e3
        const flow = {
            ...base,
            edges: [
                { id: 'x1', kind: 'output', source: 'p1', target: 'e2', timeout: '1s' },
                { id: 'x2', kind: 'output', source: 'p2', target: 'e2', timeout: '1s' },
                { id: 'x3', kind: 'output', source: 'e2', target: 'e3', timeout: '1s' },
            ],
        };
        expect(validateFlow(flow, catalog).errors).toEqual([
            { edgeId: 'x3', message: '100.64.0.1.telemetry → swap.detector: swap.detector does not accept Telemetry' },
        ]);
    });

    it('warns about engines with no input and untyped nodes', () => {
        const untyped = mapDaemons(catalog, (d) => ({ ...d, engines: [...d.engines, { name: `${d.ip}.tracker`, accepts: ['*'], events: [], typed: false }] }));
        const flow = addEngine(flowB(), untyped, `${SWAP.ip}.tracker`, AT);
        expect(validateFlow(flow, untyped).warnings).toEqual([
            { nodeId: 'e3', message: 'swap.tracker: no type information; connections are not type-checked' },
            { nodeId: 'e3', message: 'Nothing feeds into swap.tracker' },
        ]);
    });
});

const FLOW_B_TOML = `[[producers]]
name = "harpy.telemetry"
[[producers]]
name = "harpy.frames"

[[engines]]
name = "100.64.0.2.yolo"
location = "local"
[[engines]]
name = "100.64.0.1.telemetry"
location = "remote"

[[edges]]
from = "harpy.telemetry"
to = "100.64.0.1.telemetry"
timeout = "2s"
[[edges]]
from = "harpy.frames"
to = "100.64.0.2.yolo"
timeout = "1s"
[[edges]]
from = "100.64.0.2.yolo"
to = "100.64.0.1.telemetry"
timeout = "2s"
`;

describe('toTemplate', () => {
    const catalog = catalogFixture();

    it('gives every producer and engine by template name, and output edges only', () => {
        const flow = addEdge(flowB(), catalog, 'e1', 'p2', 'callback');
        expect(toTemplate(flow, catalog)).toEqual({
            vehicle: HARPY,
            producers: [{ name: 'harpy.telemetry', tokens: 1 }, { name: 'harpy.frames', tokens: 1 }],
            engines: [{ name: '100.64.0.2.yolo' }, { name: '100.64.0.1.telemetry' }],
            edges: [
                { from: 'harpy.telemetry', to: '100.64.0.1.telemetry', timeout: '2s' },
                { from: 'harpy.frames', to: '100.64.0.2.yolo', timeout: '1s' },
                { from: '100.64.0.2.yolo', to: '100.64.0.1.telemetry', timeout: '2s' },
            ],
        });
    });

    it('refuses an unreachable engine', () => {
        const flow = { ...flowB(), nodes: [...flowB().nodes, { id: 'e3', kind: 'engine', engine: yolo(LAB), position: AT }] };
        expect(() => toTemplate(flow, catalog)).toThrow('is neither on');
    });
});

describe('toFlowToml', () => {
    const catalog = catalogFixture();

    it('reproduces the reference Flow B', () => {
        expect(toFlowToml(flowB(), catalog)).toBe(FLOW_B_TOML);
    });

    it('names nodes placed from the palette by hostname, with locations relative to the vehicle', () => {
        let flow = addProducer(setVehicle(emptyFlow(), HARPY), catalog, 'frames', AT);
        flow = addEngine(flow, catalog, yolo(SWAP), AT);
        flow = addEngine(flow, catalog, `${GCS.ip}.telemetry`, AT);
        const toml = toFlowToml(addEdge(flow, catalog, 'p1', 'e1', 'output'), catalog);
        expect(toml).toContain('[[producers]]\nname = "harpy.frames"\n');
        expect(toml).toContain('[[engines]]\nname = "swap.yolo"\nlocation = "local"\n');
        expect(toml).toContain('[[engines]]\nname = "gcs.telemetry"\nlocation = "remote"\n');
    });

    it('emits tokens only when not the default', () => {
        const toml = toFlowToml(updateNode(flowB(), 'p2', { tokens: 3 }), catalog);
        expect(toml).toContain('[[producers]]\nname = "harpy.frames"\ntokens = 3\n');
        expect(toml.match(/tokens/g)).toHaveLength(1);
    });

    it('omits callbacks with a header comment', () => {
        const flow = addEdge(flowB(), catalog, 'e1', 'p2', 'callback');
        const toml = toFlowToml(flow, catalog);
        expect(toml).toBe('# 1 callback edge(s) omitted: flow TOML has no callback representation.\n\n' + FLOW_B_TOML);
    });

    it('refuses a flow with errors', () => {
        expect(() => toFlowToml(emptyFlow(), catalog)).toThrow('flow has 1 error(s); fix them before exporting TOML');
    });

    it('refuses a flow naming an unknown host', () => {
        const flow = fromTemplate({ producers: [{ name: 'swap.frames' }], engines: [{ name: 'heisenberg.yolo' }], edges: null }, catalog, HARPY);
        expect(() => toFlowToml(flow, catalog)).toThrow('flow has 1 error(s)');
    });

    it('escapes names safely', () => {
        const quoted = mapDaemons(catalog, (d) => ({
            ...d,
            vehicles: d.vehicles.map((v) => ({ ...v, streams: [...v.streams, { name: 'cam"1', output: 'image' }] })),
        }));
        const flow = addProducer(flowB(), quoted, 'cam"1', AT);
        expect(toFlowToml(flow, quoted)).toContain('name = "harpy.cam\\"1"');
    });
});

describe('fromTemplate', () => {
    const catalog = catalogFixture();
    const strip = (flow) => {
        const name = (id) => nodeLabel(flow, flow.nodes.find((x) => x.id === id));
        return {
            vehicle: flow.vehicle,
            nodes: flow.nodes.map((n) => (n.kind === 'producer'
                ? { kind: n.kind, stream: n.stream, tokens: n.tokens }
                : { kind: n.kind, engine: n.engine })),
            edges: flow.edges.map((e) => ({ from: name(e.source), to: name(e.target), timeout: e.timeout })),
        };
    };

    it('round-trips toTemplate, ignoring ids and positions', () => {
        let flow = updateNode(flowB(), 'p2', { tokens: 3 });
        const back = fromTemplate(toTemplate(flow, catalog), catalog, HARPY);
        expect(strip(back)).toEqual(strip(flow));
        expect(validateFlow(back, catalog).errors).toEqual([]);
    });

    it('takes the stream from producer names, keeping engine names', () => {
        const flow = fromTemplate({
            producers: [{ name: '100.64.0.2.frames', tokens: 2 }],
            engines: [{ name: 'gcs.telemetry' }],
            edges: [{ from: '100.64.0.2.frames', to: 'gcs.telemetry', timeout: '1s' }],
        }, catalog, HARPY);
        expect(flow.nodes.map((n) => [n.stream, n.engine])).toEqual([['frames', undefined], [undefined, 'gcs.telemetry']]);
        expect(flow.nodes[0]).not.toHaveProperty('host');
        expect(flow.nodes[0].tokens).toBe(2);
        expect(flow.edges).toEqual([{ id: 'x1', kind: 'output', source: 'p1', target: 'e1', timeout: '1s' }]);
    });

    it('loads a flow saved with addresses once the daemon has a hostname', () => {
        const flow = fromTemplate({
            producers: [{ name: '100.64.0.2.frames' }], engines: [{ name: '100.64.0.2.yolo' }],
            edges: [{ from: '100.64.0.2.frames', to: '100.64.0.2.yolo', timeout: '1s' }],
        }, catalog, HARPY);
        expect(validateFlow(flow, catalog)).toEqual({ errors: [], warnings: [] });
    });

    it('ignores a producer name\'s prefix: any vehicle\'s stream becomes the flow vehicle\'s', () => {
        const flow = fromTemplate({ producers: [{ name: 'alpha.frames' }], engines: null, edges: null }, catalog, HARPY);
        expect(flow.nodes[0]).toEqual(expect.objectContaining({ kind: 'producer', stream: 'frames' }));
        expect(flow.nodes[0]).not.toHaveProperty('host');
        expect(nodeLabel(flow, flow.nodes[0])).toBe('harpy.frames');
        expect(validateFlow(flow, catalog).errors).toEqual([]);
    });

    it('qualifies the bare names of an old TOML export with the vehicle\'s daemon', () => {
        const flow = fromTemplate({
            producers: [{ name: 'frames' }], engines: [{ name: 'yolo' }],
            edges: [{ from: 'frames', to: 'yolo', timeout: '1s' }],
        }, catalog, HARPY);
        const home = hostLabel(catalog, findDaemon(catalog, daemonOf(HARPY)));
        expect(flow.nodes.map((n) => n.stream ?? n.engine)).toEqual(['frames', `${home}.yolo`]);
        expect(flow.edges).toHaveLength(1);
    });

    it('keeps a bare producer name as the stream and flags it without a vehicle', () => {
        const flow = fromTemplate({ producers: [{ name: 'frames' }], engines: null, edges: null }, catalog, '');
        expect(flow.nodes[0]).toMatchObject({ stream: 'frames' });
        expect(validateFlow(flow, catalog).errors).toContainEqual({ nodeId: 'p1', message: 'frames: no vehicle selected' });
    });

    it.each([
        ['an unknown host', { ...catalogFixture() }, 'heisenberg.yolo: flow host heisenberg is not a known daemon'],
        ['an unavailable daemon', { ...catalogFixture(), unavailable: [HEISENBERG] }, 'heisenberg.yolo: daemon heisenberg is unavailable: refused'],
    ])('loads %s and flags that node alone', (_, cat, message) => {
        const flow = fromTemplate({
            producers: [{ name: 'swap.frames' }], engines: [{ name: 'heisenberg.yolo' }],
            edges: [{ from: 'swap.frames', to: 'heisenberg.yolo', timeout: '1s' }],
        }, cat, HARPY);
        expect(validateFlow(flow, cat).errors).toEqual([{ nodeId: 'e1', message }]);
    });

    it('reports an engine named twice under different spellings', () => {
        const flow = fromTemplate({ producers: null, engines: [{ name: 'swap.yolo' }, { name: '100.64.0.2.yolo' }], edges: null }, catalog, HARPY);
        expect(validateFlow(flow, catalog).errors).toEqual([{ nodeId: 'e2', message: '100.64.0.2.yolo appears more than once' }]);
    });

    it('lays nodes out in columns, producers first', () => {
        const back = fromTemplate(toTemplate(flowB(), catalog), catalog, HARPY);
        const x = (id) => back.nodes.find((n) => n.id === id).position.x;
        const ids = back.nodes.map((n) => n.id);
        expect(new Set(ids).size).toBe(ids.length);
        const [t, f] = back.nodes.filter((n) => n.kind === 'producer');
        const [yolo, tel] = back.nodes.filter((n) => n.kind === 'engine');
        expect(t.position.x).toBe(f.position.x);
        expect(yolo.position.x).toBeGreaterThan(t.position.x);
        expect(tel.position.x).toBeGreaterThan(yolo.position.x);
        expect(x(t.id)).toBe(0);
    });
});

describe('fromTemplate with null lists', () => {
    const catalog = catalogFixture();

    it('accepts a producers-only template (engines and edges null)', () => {
        const flow = fromTemplate({ vehicle: '', producers: [{ name: 'swap.frames' }], engines: null, edges: null }, catalog, HARPY);
        expect(flow.nodes).toHaveLength(1);
        expect(flow.edges).toEqual([]);
    });

    it('accepts a template with no edges', () => {
        const flow = fromTemplate({
            vehicle: '', producers: [{ name: 'swap.frames' }], engines: [{ name: 'swap.yolo' }], edges: null,
        }, catalog, HARPY);
        expect(flow.nodes).toHaveLength(2);
        expect(flow.edges).toEqual([]);
    });

    it('accepts an engines-only template (producers null)', () => {
        const flow = fromTemplate({ vehicle: '', producers: null, engines: [{ name: 'swap.yolo' }], edges: null }, catalog, HARPY);
        expect(flow.nodes).toHaveLength(1);
    });
});

describe('importFlowToml', () => {
    const catalog = catalogFixture();
    afterEach(() => vi.unstubAllGlobals());
    const stub = (status, body) => {
        vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs:8000' } });
        const fetch = vi.fn(async () => ({ status, json: async () => body }));
        vi.stubGlobal('fetch', fetch);
        return fetch;
    };

    it('marks unknown engines', async () => {
        const fetch = stub(200, {
            vehicle: '',
            producers: [{ name: 'swap.frames' }],
            engines: [{ name: 'swap.ghost' }],
            edges: [{ from: 'swap.frames', to: 'swap.ghost', timeout: '1s' }],
        });
        const flow = await importFlowToml('x', catalog, HARPY);
        expect(fetch.mock.calls[0][0]).toBe('http://gcs:8000/api/flows/from_toml');
        expect(flow.nodes.some((n) => n.engine === 'swap.ghost')).toBe(true);
        expect(validateFlow(flow, catalog).errors.some((e) => e.message.includes('ghost'))).toBe(true);
    });

    it('throws the server detail on a 422', async () => {
        stub(422, { detail: 'unknown key "bogus"' });
        await expect(importFlowToml('x', catalog, HARPY)).rejects.toThrow('unknown key "bogus"');
    });
});

describe('filters', () => {
    // An H.264-encoder-like filter on the vehicle's daemon (takes RawFrames,
    // forwards EncodedFrames it doesn't declare) feeding a sink that takes
    // only EncodedFrames.
    const catalog = catalogFixture();
    catalog.daemons[1].engines.push({ name: `${SWAP.ip}.enc`, category: 'filter', accepts: [TYPES.raw], events: [{ label: 'result', type: null }], typed: true, problem: null });
    catalog.daemons[0].engines.push({ name: `${GCS.ip}.sink`, category: 'generic', accepts: [TYPES.encoded], events: [{ label: 'result', type: null }], typed: true, problem: null });
    const chain = () => {
        let flow = { ...emptyFlow(), vehicle: HARPY };
        flow = addProducer(flow, catalog, 'frames', { x: 0, y: 0 });
        flow = addEngine(flow, catalog, `${SWAP.ip}.enc`, { x: 0, y: 0 });
        flow = addEngine(flow, catalog, `${GCS.ip}.sink`, { x: 0, y: 0 });
        flow = addEdge(flow, catalog, 'p1', 'e1', 'output');
        return addEdge(flow, catalog, 'e1', 'e2', 'output');
    };

    it('forward an unknown type, so what follows them is not type-checked', () => {
        const flow = chain();
        expect(forwardedTypes(flow, catalog, 'e1')).toEqual(['*']);
        const { errors, warnings } = validateFlow(flow, catalog);
        expect(errors).toEqual([]);
        expect(warnings).toContainEqual({
            edgeId: 'x2',
            message: 'swap.enc → gcs.sink: swap.enc is a filter, so what it forwards is not type-checked',
        });
    });

    it('still type-check their own input', () => {
        let flow = { ...emptyFlow(), vehicle: HARPY };
        flow = addProducer(flow, catalog, 'telemetry', { x: 0, y: 0 });
        flow = addEngine(flow, catalog, `${SWAP.ip}.enc`, { x: 0, y: 0 });
        expect(canConnect(flow, catalog, 'p1', 'e1', 'output')).not.toBeNull();
    });
});
