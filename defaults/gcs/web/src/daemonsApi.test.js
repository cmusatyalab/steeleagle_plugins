import { describe, it, expect, afterEach, vi } from 'vitest';
import {
    applyConfig,
    checkConfig,
    convertConfig,
    daemonName,
    fetchConfig,
    fetchDaemons,
    fetchPackages,
    installPackage,
    removePackage,
    restartDaemon,
    restartService,
} from './daemonsApi.js';

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

function call(fetch) {
    const [url, init] = fetch.mock.calls[0];
    return { url, method: init?.method, body: init?.body === undefined ? undefined : JSON.parse(init.body) };
}

describe('config and package requests', () => {
    it('fetchConfig gets the config by daemon ip', async () => {
        const fetch = stubFetch(200, { raw_toml: 'x', config_hash: 'h', form: null });
        expect(await fetchConfig('100.64.0.2')).toEqual({ config: { raw_toml: 'x', config_hash: 'h', form: null }, error: null });
        expect(call(fetch)).toEqual({ url: 'http://gcs:8002/api/daemons/config?ip=100.64.0.2', method: 'GET', body: undefined });
    });

    it('convertConfig posts the body as is', async () => {
        const fetch = stubFetch(200, { raw_toml: 'x' });
        expect(await convertConfig({ to: 'raw', form: {} })).toEqual({ result: { raw_toml: 'x' }, error: null });
        expect(call(fetch)).toEqual({ url: 'http://gcs:8002/api/daemons/config/convert', method: 'POST', body: { to: 'raw', form: {} } });
    });

    it('convertConfig describes a TOML error', async () => {
        stubFetch(422, { detail: { message: 'bad', line: 1, column: 2 } });
        const { result, error } = await convertConfig({ to: 'form', raw_toml: '[' });
        expect(result).toBeNull();
        expect(error).toContain('bad');
    });

    it('checkConfig posts the ip and payload', async () => {
        const fetch = stubFetch(200, { errors: [], warnings: ['w'], blocking: false });
        expect(await checkConfig('100.64.0.2', { raw_toml: 'x' })).toEqual({ result: { errors: [], warnings: ['w'], blocking: false }, error: null });
        expect(call(fetch)).toEqual({ url: 'http://gcs:8002/api/daemons/config/check', method: 'POST', body: { ip: '100.64.0.2', raw_toml: 'x' } });
    });

    it('applyConfig puts the ip, hash and payload', async () => {
        const fetch = stubFetch(200, { ok: true });
        expect(await applyConfig('100.64.0.2', 'h', { form: {} })).toEqual({ ok: true, conflict: false, errors: null, error: null, networkError: false });
        expect(call(fetch)).toEqual({ url: 'http://gcs:8002/api/daemons/config', method: 'PUT', body: { ip: '100.64.0.2', config_hash: 'h', form: {} } });
    });

    it('applyConfig reports a conflict', async () => {
        stubFetch(409, { conflict: true, detail: 'the config changed since it was loaded' });
        expect(await applyConfig('ip', 'h', { form: {} })).toEqual({ ok: false, conflict: true, errors: null, error: 'the config changed since it was loaded', networkError: false });
    });

    it('applyConfig reports a refusal with its errors', async () => {
        stubFetch(422, { detail: 'refused', errors: ['e'] });
        expect(await applyConfig('ip', 'h', { form: {} })).toEqual({ ok: false, conflict: false, errors: ['e'], error: 'refused', networkError: false });
    });

    it('applyConfig reports a lost connection', async () => {
        vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs:8002' } });
        vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('Failed to fetch'); }));
        const out = await applyConfig('ip', 'h', { form: {} });
        expect(out.ok).toBe(false);
        expect(out.networkError).toBe(true);
    });

    it('restartDaemon posts the ip', async () => {
        const fetch = stubFetch(200, { ok: true });
        expect(await restartDaemon('100.64.0.2')).toEqual({ ok: true, error: null, networkError: false });
        expect(call(fetch)).toEqual({ url: 'http://gcs:8002/api/daemons/restart', method: 'POST', body: { ip: '100.64.0.2' } });
    });

    it('fetchPackages gets the packages by daemon ip', async () => {
        const fetch = stubFetch(200, { packages: [{ uri: 'u', plugins: [] }] });
        expect(await fetchPackages('100.64.0.2')).toEqual({ packages: [{ uri: 'u', plugins: [] }], error: null });
        expect(call(fetch).url).toBe('http://gcs:8002/api/daemons/packages?ip=100.64.0.2');
    });

    it('installPackage and removePackage post the ip and uri', async () => {
        let fetch = stubFetch(200, { ok: true });
        expect(await installPackage('100.64.0.2', 'github.com/org/pkg')).toBeNull();
        expect(call(fetch)).toEqual({ url: 'http://gcs:8002/api/daemons/packages/install', method: 'POST', body: { ip: '100.64.0.2', uri: 'github.com/org/pkg' } });
        fetch = stubFetch(404, { detail: 'no package u' });
        expect(await removePackage('100.64.0.2', 'u')).toBe('no package u');
        expect(call(fetch)).toEqual({ url: 'http://gcs:8002/api/daemons/packages/remove', method: 'POST', body: { ip: '100.64.0.2', uri: 'u' } });
    });
});
