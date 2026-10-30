import { describe, it, expect, afterEach, vi } from 'vitest';
import { catalogFixture, flowB, HARPY } from './flowFixtures.js';
import { emptyFlow, toTemplate, validateFlow } from './flowModel.js';
import { pushButtonState, pushFlow } from './flowPush.js';

afterEach(() => {
    vi.unstubAllGlobals();
});

function stubFetch(status, body) {
    vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs:8002' } });
    const fetch = vi.fn(async () => ({ status, json: async () => body }));
    vi.stubGlobal('fetch', fetch);
    return fetch;
}

describe('pushButtonState', () => {
    const catalog = catalogFixture();
    const state = (flow, cat = catalog) => pushButtonState(flow, cat, cat ? validateFlow(flow, cat) : { errors: [], warnings: [] });

    it("names the flow's vehicle, and is enabled for a valid flow", () => {
        expect(state(flowB())).toEqual({ label: 'Push flow to harpy', disabled: false, tooltip: undefined });
    });

    it('is disabled until a vehicle is picked', () => {
        expect(state(emptyFlow())).toMatchObject({ label: 'Push flow', disabled: true, tooltip: 'Select a vehicle first' });
    });

    it('is disabled until the catalog loads', () => {
        expect(state(flowB(), null)).toMatchObject({ disabled: true, tooltip: 'Loading the flow catalog' });
    });

    it('is disabled for an empty flow', () => {
        expect(state({ ...flowB(), nodes: [], edges: [] })).toMatchObject({ disabled: true, tooltip: 'Add nodes to the flow first' });
    });

    it('is disabled while the flow has errors', () => {
        const broken = { ...flowB(), edges: flowB().edges.map((e) => ({ ...e, timeout: 'soon' })) };
        expect(validateFlow(broken, catalog).errors.length).toBeGreaterThan(0);
        expect(state(broken)).toMatchObject({ disabled: true, tooltip: 'Fix the errors below first' });
    });
});

describe('pushFlow', () => {
    const catalog = catalogFixture();

    it("posts the flow's template, and resolves to null", async () => {
        const fetch = stubFetch(200, { vehicle: HARPY });
        expect(await pushFlow(flowB(), catalog)).toBeNull();
        const [url, init] = fetch.mock.calls[0];
        expect(url).toBe('http://gcs:8002/api/flows/push');
        expect(init.method).toBe('POST');
        expect(JSON.parse(init.body)).toEqual({ flow: toTemplate(flowB(), catalog) });
    });

    it("resolves to the server's reason on failure", async () => {
        stubFetch(422, { detail: 'flow needs camera frames, which this vehicle lacks' });
        expect(await pushFlow(flowB(), catalog)).toBe('flow needs camera frames, which this vehicle lacks');
    });

    it("resolves to why the template can't be built, without posting", async () => {
        const fetch = stubFetch(200, {});
        const flow = { ...flowB(), nodes: [...flowB().nodes, { id: 'e3', kind: 'engine', engine: '100.64.0.3.yolo', position: { x: 0, y: 0 } }] };
        expect(await pushFlow(flow, catalog)).toMatch('is neither on');
        expect(fetch).not.toHaveBeenCalled();
    });
});
