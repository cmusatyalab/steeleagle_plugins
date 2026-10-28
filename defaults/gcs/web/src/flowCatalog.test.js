import { describe, it, expect, afterEach, vi } from 'vitest';
import {
    createCatalogLoader, engineLocation, fetchCatalog, findEngine, findVehicle, orchestrator,
    findTemplateEngine, hostLabel, resolveHost, resolveNode, serviceName, shortType, splitTemplateName, templateName, vehicleLabel, vehicleOptions,
} from './flowCatalog.js';
import { GCS, HARPY, HEISENBERG, LAB, MOCK2, MOCK3, SWAP, TYPES, catalogFixture, flowB } from './flowFixtures.js';

afterEach(() => {
    vi.unstubAllGlobals();
});

function stubFetch(impl) {
    vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs:8000' } });
    const fetch = vi.fn(impl);
    vi.stubGlobal('fetch', fetch);
    return fetch;
}

const reply = (status, body) => async () => ({ status, json: async () => body });

describe('fetchCatalog', () => {
    it('gets the catalog route and returns its daemons and unavailable list', async () => {
        const fetch = stubFetch(reply(200, {
            daemons: [{ ip: GCS.ip, vehicles: [], engines: [] }],
            unavailable: [{ ip: LAB.ip, hostname: 'lab', reason: 'timeout' }],
        }));
        const result = await fetchCatalog();
        expect(fetch.mock.calls[0][0]).toBe('http://gcs:8000/api/flows/catalog');
        expect(result).toEqual({
            daemons: [{ ip: GCS.ip, vehicles: [], engines: [] }],
            unavailable: [{ ip: LAB.ip, hostname: 'lab', reason: 'timeout' }],
            error: null,
        });
    });

    it('reports the server\'s reason when there is no daemon or listing fails', async () => {
        stubFetch(reply(503, { detail: 'the GCS has no connection to the daemon' }));
        expect(await fetchCatalog()).toEqual({ daemons: [], unavailable: [], error: 'the GCS has no connection to the daemon' });
        stubFetch(reply(502, { detail: 'listing failed' }));
        expect((await fetchCatalog()).error).toBe('listing failed');
    });

    it('reports a network failure', async () => {
        stubFetch(async () => { throw new Error('offline'); });
        const result = await fetchCatalog();
        expect(result.daemons).toEqual([]);
        expect(result.error).toContain('offline');
    });
});

describe('createCatalogLoader', () => {
    it('ignores a slower, older response that resolves after a newer one', async () => {
        const resolvers = [];
        vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs:8000' } });
        vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => resolvers.push(resolve))));
        const seen = [];
        const load = createCatalogLoader((c) => seen.push(c.daemons.map((d) => d.ip)));
        const first = load();
        const second = load();
        const answer = (ip) => ({ status: 200, json: async () => ({ daemons: [{ ip, vehicles: [], engines: [] }], unavailable: [] }) });
        resolvers[1](answer('new'));
        await second;
        resolvers[0](answer('old'));
        await first;
        expect(seen).toEqual([['new']]);
    });

    it('keeps the last good catalog when a refresh fails, recording the error', async () => {
        const good = { daemons: [{ ip: GCS.ip, vehicles: [], engines: [] }], unavailable: [{ ip: LAB.ip, reason: 'timeout' }] };
        let respond = reply(200, good);
        stubFetch((...args) => respond(...args));
        const seen = [];
        const load = createCatalogLoader((c) => seen.push(c));
        await load();
        respond = reply(503, { detail: 'no daemon connection' });
        await load();
        expect(seen[1]).toEqual({ ...good, error: 'no daemon connection' });
        respond = reply(200, good);
        await load();
        expect(seen[2].error).toBeNull();
    });

    it('delivers the failure as is when no catalog ever loaded', async () => {
        stubFetch(reply(503, { detail: 'no daemon connection' }));
        const seen = [];
        await createCatalogLoader((c) => seen.push(c))();
        expect(seen).toEqual([{ daemons: [], unavailable: [], error: 'no daemon connection' }]);
    });
});

describe('resolveNode', () => {
    const catalog = catalogFixture();
    const flow = flowB();

    it('resolves a producer to its stream type and camera details', () => {
        const r = resolveNode(flow, catalog, flow.nodes[1]);
        expect(r).toMatchObject({
            resolved: true, reason: null, output: TYPES.raw, accepts: [], events: [], eventTypes: {},
            category: null, typed: true, problem: null,
        });
        expect(r.stream).toMatchObject({ name: 'frames', kind: 'camera', primary: true, hres: 1080, vres: 720, fps: 30 });
    });

    it('resolves an engine to its accepts, event labels, event types and category', () => {
        expect(resolveNode(flow, catalog, flow.nodes[2])).toEqual({
            resolved: true, reason: null, output: null, stream: null,
            accepts: [TYPES.raw], events: ['result', 'dropped'],
            eventTypes: { result: TYPES.detection, dropped: TYPES.generic },
            category: 'detection', typed: true, problem: null, location: 'local',
        });
        expect(resolveNode(flow, catalog, flow.nodes[3]).location).toBe('remote');
    });

    it('passes an untyped entry\'s problem through', () => {
        const untyped = {
            ...catalog,
            daemons: catalog.daemons.map((d) => ({
                ...d,
                engines: d.engines.map((e) => (e.name === `${SWAP.ip}.yolo`
                    ? { ...e, typed: false, problem: 'no plugin file', accepts: ['*'], events: [{ label: 'result', type: null }] }
                    : e)),
            })),
        };
        expect(resolveNode(flow, untyped, flow.nodes[2])).toMatchObject({
            typed: false, problem: 'no plugin file', events: ['result'], eventTypes: { result: null },
        });
    });

    it('gives unresolved nodes the same keys', () => {
        expect(resolveNode({ ...flow, vehicle: null }, catalog, flow.nodes[0])).toEqual({
            resolved: false, reason: 'no vehicle selected', output: null, stream: null, accepts: [], events: [],
            eventTypes: {}, category: null, typed: true, problem: null,
        });
    });

    it('explains why a node is unresolved', () => {
        const gone = { ...flow.nodes[2], engine: `${SWAP.ip}.tracker` };
        expect(resolveNode(flow, catalog, gone).reason).toBe(`${SWAP.ip}.tracker: no such engine on ${SWAP.ip}`);
        const noStream = { ...flow.nodes[0], stream: 'lidar' };
        expect(resolveNode(flow, catalog, noStream).reason).toBe('harpy has no stream lidar');
        expect(resolveNode({ ...flow, vehicle: `${SWAP.ip}.osprey` }, catalog, flow.nodes[0]).reason).toBe(`osprey: no such vehicle on ${SWAP.ip}`);
        const unknown = { ...gone, engine: 'nowhere.yolo' };
        expect(resolveNode(flow, catalog, unknown).reason).toBe('flow host nowhere is not a known daemon');
        expect(resolveNode(flow, catalog, { ...gone, engine: 'yolo' }).reason).toBe('flow name yolo has no host');
        const down = { ...catalog, unavailable: [{ ip: '100.64.0.9', hostname: 'heisenberg', reason: 'refused' }] };
        expect(resolveNode(flow, down, { ...gone, engine: 'heisenberg.yolo' }).reason).toBe('daemon heisenberg is unavailable: refused');
    });

    it('explains an unresolved producer', () => {
        expect(resolveNode({ ...flow, vehicle: null }, catalog, flow.nodes[0]).reason).toBe('no vehicle selected');
        expect(resolveNode(flow, catalog, { ...flow.nodes[0], stream: 'lidar' }).reason).toBe('harpy has no stream lidar');
        expect(resolveNode(flow, catalog, { ...flow.nodes[0], host: 'nope' }).resolved).toBe(true);
    });

    it('resolves hostnames in template names', () => {
        const byName = { ...flow.nodes[2], engine: 'swap.yolo' };
        expect(resolveNode(flow, catalog, byName)).toMatchObject({ resolved: true, location: 'local' });
        expect(resolveNode(flow, catalog, { ...byName, engine: 'gcs.yolo' }).location).toBe('remote');
    });

    it('resolves an engine on a third daemon with no location', () => {
        const third = { id: 'e9', kind: 'engine', engine: `${LAB.ip}.yolo`, position: { x: 0, y: 0 } };
        expect(resolveNode(flow, catalog, third)).toMatchObject({ resolved: true, location: null });
    });
});

describe('host resolution', () => {
    const catalog = catalogFixture();
    const withMocks = { ...catalog, daemons: [...catalog.daemons, { ...MOCK2, vehicles: [], engines: [] }, { ...MOCK3, vehicles: [], engines: [] }] };
    const down = { ...catalog, unavailable: [HEISENBERG] };

    it('splits at the last dot', () => {
        expect(splitTemplateName('100.64.0.2.yolo')).toEqual({ host: '100.64.0.2', bare: 'yolo' });
        expect(splitTemplateName('a.b.ts.net.yolo')).toEqual({ host: 'a.b.ts.net', bare: 'yolo' });
        for (const bad of ['yolo', '.yolo', 'swap.', '', null]) expect(splitTemplateName(bad)).toBeNull();
    });

    it('resolves IPs, hostnames and first labels, ignoring case', () => {
        expect(resolveHost(catalog, SWAP.ip).daemon.ip).toBe(SWAP.ip);
        expect(resolveHost(catalog, 'SWAP').daemon.ip).toBe(SWAP.ip);
        const fqdn = { ...catalog, daemons: catalog.daemons.map((d) => (d.ip === SWAP.ip ? { ...d, hostname: 'swap.tail1234.ts.net' } : d)) };
        expect(resolveHost(fqdn, 'swap').daemon.ip).toBe(SWAP.ip);
        expect(resolveHost(fqdn, 'Swap.TAIL1234.ts.net').daemon.ip).toBe(SWAP.ip);
        // an unlisted IP is not a short name
        expect(resolveHost(catalog, '100.64.0.77').error).toBe('flow host 100.64.0.77 is not a known daemon');
    });

    it('lets an exact hostname beat a first-label match, and reports ambiguity', () => {
        expect(resolveHost(withMocks, MOCK2.hostname).daemon.ip).toBe(MOCK2.ip);
        expect(resolveHost(withMocks, MOCK3.hostname).daemon.ip).toBe(MOCK3.ip);
        expect(resolveHost(withMocks, 'mock-100').error).toBe('flow host mock-100 is ambiguous: it matches 100.64.0.12 and 100.64.0.13');
        const twins = { ...catalog, daemons: catalog.daemons.map((d) => (d.ip === LAB.ip ? { ...d, hostname: 'swap.other.net' } : d)) };
        expect(resolveHost(twins, 'swap.third.net').error).toBe('flow host swap.third.net is ambiguous: it matches 100.64.0.2 and 100.64.0.3');
    });

    it('explains a host that is only unavailable', () => {
        expect(resolveHost(down, 'heisenberg').error).toBe('daemon heisenberg is unavailable: refused');
        expect(resolveHost(down, '100.64.0.9').error).toBe('daemon heisenberg is unavailable: refused');
        expect(resolveHost(down, 'heisenberg.tail.ts.net').error).toBe('daemon heisenberg is unavailable: refused');
    });

    it('labels a daemon by hostname only if it resolves back to it', () => {
        expect(hostLabel(catalog, catalog.daemons[1])).toBe('swap');
        expect(hostLabel(catalog, { ip: '100.64.0.5' })).toBe('100.64.0.5');
        const dup = { ...catalog, daemons: catalog.daemons.map((d) => (d.ip === LAB.ip ? { ...d, hostname: 'swap' } : d)) };
        expect(hostLabel(dup, dup.daemons[1])).toBe(SWAP.ip);
        expect(hostLabel(withMocks, withMocks.daemons[3])).toBe(MOCK2.hostname);
    });

    it('labels by IP a daemon whose hostname clashes with an unavailable one', () => {
        const clash = {
            daemons: [{ ip: '100.64.0.2', hostname: 'eagled' }, { ip: '100.64.0.4', hostname: 'other' }],
            unavailable: [{ ip: '100.64.0.3', hostname: 'Eagled.tail1.ts.net', reason: 'refused' }],
        };
        expect(hostLabel(clash, clash.daemons[0])).toBe('100.64.0.2');
        expect(hostLabel(clash, clash.daemons[1])).toBe('other');
    });

    it('converts between service names and template names', () => {
        expect(templateName(catalog, `${SWAP.ip}.yolo`)).toBe('swap.yolo');
        expect(templateName(catalog, '100.64.0.77.yolo')).toBe('100.64.0.77.yolo');
        expect(serviceName(catalog, 'swap.yolo')).toEqual({ name: `${SWAP.ip}.yolo`, error: null });
        // a flow saved with an IP host still resolves once the daemon reports a hostname
        expect(serviceName(catalog, `${SWAP.ip}.yolo`).name).toBe(`${SWAP.ip}.yolo`);
        expect(serviceName(catalog, 'nope.yolo').error).toBe('flow host nope is not a known daemon');
        expect(serviceName(catalog, 'yolo').error).toBe('flow name yolo has no host');
        expect(findTemplateEngine(catalog, 'lab.yolo').name).toBe(`${LAB.ip}.yolo`);
        expect(findTemplateEngine(catalog, 'lab.nope')).toBeNull();
        expect(findTemplateEngine(catalog, 'x.yolo')).toBeNull();
    });
});

describe('catalog lookups', () => {
    const catalog = catalogFixture();

    it('finds vehicles, engines and the orchestrator by full name', () => {
        expect(findVehicle(catalog, HARPY).name).toBe(HARPY);
        expect(findVehicle(catalog, null)).toBeNull();
        expect(findVehicle(catalog, 'harpy')).toBeNull();
        expect(findEngine(catalog, `${LAB.ip}.yolo`).name).toBe(`${LAB.ip}.yolo`);
        expect(findEngine(catalog, `${LAB.ip}.nope`)).toBeNull();
        expect(orchestrator(catalog).ip).toBe(GCS.ip);
        expect(orchestrator({ daemons: [] })).toBeNull();
    });

    it('derives engine location: local, remote or unreachable', () => {
        expect(engineLocation(catalog, HARPY, `${SWAP.ip}.yolo`)).toBe('local');
        expect(engineLocation(catalog, HARPY, `${GCS.ip}.yolo`)).toBe('remote');
        expect(engineLocation(catalog, HARPY, `${LAB.ip}.yolo`)).toBeNull();
        // a vehicle on the orchestrator's own daemon: its engines are local
        expect(engineLocation(catalog, `${GCS.ip}.cassowary`, `${GCS.ip}.yolo`)).toBe('local');
        // no vehicle: only the orchestrator is known
        expect(engineLocation(catalog, null, `${GCS.ip}.yolo`)).toBe('remote');
        expect(engineLocation(catalog, null, `${SWAP.ip}.yolo`)).toBeNull();
        // hostnames work too
        expect(engineLocation(catalog, HARPY, 'swap.yolo')).toBe('local');
        expect(engineLocation(catalog, HARPY, 'GCS.yolo')).toBe('remote');
        expect(engineLocation(catalog, HARPY, 'nope.yolo')).toBeNull();
        expect(engineLocation({ daemons: [] }, HARPY, `${GCS.ip}.yolo`)).toBeNull();
    });
});

describe('vehicles', () => {
    const catalog = catalogFixture();

    it('offers every vehicle by full name, labelled without the daemon', () => {
        const options = vehicleOptions(catalog, null);
        expect(options).toEqual([HARPY, `${SWAP.ip}.kestrel`]);
        expect(options.map((o) => vehicleLabel(o, options))).toEqual(['harpy', 'kestrel']);
    });

    it('names a vehicle by its daemon only when another daemon has one of the same name', () => {
        const options = [HARPY, `${LAB.ip}.harpy`, `${SWAP.ip}.kestrel`];
        expect(options.map((o) => vehicleLabel(o, options))).toEqual([`harpy @ ${SWAP.ip}`, `harpy @ ${LAB.ip}`, 'kestrel']);
    });

    it('keeps the current binding as an option even when the catalog lacks it', () => {
        const current = `${LAB.ip}.gone`;
        expect(vehicleOptions(catalog, current)).toContain(current);
    });
});

describe('shortType', () => {
    it('shortens protobuf type paths', () => {
        expect(shortType(TYPES.raw)).toBe('RawFrame');
        expect(shortType('*')).toBe('*');
        expect(shortType(null)).toBe('—');
        expect(shortType(undefined)).toBe('—');
    });
});
