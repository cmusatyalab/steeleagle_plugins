import { afterEach, describe, expect, it, vi } from 'vitest';
import { defaultFlowLoaded, defaultFlowVehicle, fetchDefaultFlow, markDefaultFlowLoaded } from './defaultFlow.js';

const catalog = {
    daemons: [
        { ip: '100.64.0.1', vehicles: [] },
        { ip: '100.64.0.2', vehicles: [{ name: '100.64.0.2.drone' }, { name: '100.64.0.2.rover' }] },
    ],
};

describe('defaultFlowVehicle', () => {
    it('finds the named vehicle by its full or bare name', () => {
        expect(defaultFlowVehicle(catalog, '100.64.0.2.rover', null)).toBe('100.64.0.2.rover');
        expect(defaultFlowVehicle(catalog, 'rover', '100.64.0.2.drone')).toBe('100.64.0.2.rover');
    });
    it('is null until the named vehicle is running', () => {
        expect(defaultFlowVehicle(catalog, 'harpy', '100.64.0.2.drone')).toBeNull();
        expect(defaultFlowVehicle({ daemons: [] }, '', null)).toBeNull();
    });
    it("without a name, keeps the Flow Builder's running vehicle, else the first", () => {
        expect(defaultFlowVehicle(catalog, '', '100.64.0.2.rover')).toBe('100.64.0.2.rover');
        expect(defaultFlowVehicle(catalog, '', '100.64.0.9.gone')).toBe('100.64.0.2.drone');
        expect(defaultFlowVehicle(catalog, '', null)).toBe('100.64.0.2.drone');
    });
});

function memoryStorage() {
    const items = new Map();
    return {
        getItem: (k) => (items.has(k) ? items.get(k) : null),
        setItem: (k, v) => { items.set(k, String(v)); },
    };
}

afterEach(() => vi.unstubAllGlobals());

function stubFetch(status, body) {
    vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs:8000' } });
    vi.stubGlobal('fetch', vi.fn(async () => ({ status, json: async () => body })));
}

describe('default flow versions', () => {
    it('are each loaded once', () => {
        vi.stubGlobal('localStorage', memoryStorage());
        expect(defaultFlowLoaded('a1')).toBe(false);
        markDefaultFlowLoaded('a1');
        expect(defaultFlowLoaded('a1')).toBe(true);
        expect(defaultFlowLoaded('b2')).toBe(false);
    });
});

describe('fetchDefaultFlow', () => {
    it('returns the default flow', async () => {
        const body = { configured: true, name: 'planning_flow', vehicle: 'drone', toml: 'x', version: 'v1' };
        stubFetch(200, body);
        expect(await fetchDefaultFlow()).toEqual(body);
    });
    it("says why it couldn't be had", async () => {
        stubFetch(422, { detail: 'default flow x: bad' });
        const result = await fetchDefaultFlow();
        expect(result.configured).toBe(false);
        expect(result.error).toContain('bad');
    });
});

describe('without storage', () => {
    it('loads every time', () => {
        vi.stubGlobal('localStorage', { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } });
        markDefaultFlowLoaded('a1');
        expect(defaultFlowLoaded('a1')).toBe(false);
    });
});
