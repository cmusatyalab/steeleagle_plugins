import { describe, it, expect, afterEach, vi } from 'vitest';
import { daemonName, fetchDaemons, restartService } from './daemonsApi.js';

afterEach(() => {
    vi.unstubAllGlobals();
});

function stubFetch(status, body) {
    vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs:8002' } });
    const fetch = vi.fn(async () => ({ status, json: async () => body }));
    vi.stubGlobal('fetch', fetch);
    return fetch;
}

describe('fetchDaemons', () => {
    it('returns the daemons', async () => {
        stubFetch(200, { daemons: [{ ip: '100.64.0.1', local: true, services: [] }] });
        expect(await fetchDaemons()).toEqual({ daemons: [{ ip: '100.64.0.1', local: true, services: [] }], error: null });
    });

    it("returns the server's reason, and no daemons, on failure", async () => {
        stubFetch(503, { detail: 'the GCS has no connection to the daemon' });
        expect(await fetchDaemons()).toEqual({ daemons: null, error: 'the GCS has no connection to the daemon' });
    });
});

describe('restartService', () => {
    it('posts the full service name', async () => {
        const fetch = stubFetch(200, { ok: true });
        expect(await restartService('100.64.0.2.yolo')).toBeNull();
        const [url, init] = fetch.mock.calls[0];
        expect(url).toBe('http://gcs:8002/api/daemons/restart_service');
        expect(JSON.parse(init.body)).toEqual({ name: '100.64.0.2.yolo' });
    });

    it("returns the daemon's reason on failure", async () => {
        stubFetch(502, { detail: 'rpc error: code = NotFound desc = no service "x"' });
        expect(await restartService('x')).toBe('rpc error: code = NotFound desc = no service "x"');
    });
});

describe('daemonName', () => {
    it('prefers the hostname', () => {
        expect(daemonName({ ip: '100.64.0.2', hostname: 'swap03' })).toBe('swap03');
        expect(daemonName({ ip: '100.64.0.2', hostname: '' })).toBe('100.64.0.2');
    });
});
