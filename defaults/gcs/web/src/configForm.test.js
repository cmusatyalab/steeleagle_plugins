import { describe, it, expect } from 'vitest';
import {
    SERVICE_TYPES,
    applyOutcome,
    argsState,
    argsValue,
    formToState,
    guards,
    pluginSpec,
    restartWarning,
    serviceKeyByName,
    stateToForm,
} from './configForm.js';

const GCS_PKG = 'https://github.com/cmusatyalab/steeleagle_plugins//defaults/gcs@v4_backend_rework';

const gcsSpec = {
    name: 'gcs',
    type: 'engine',
    args: [
        { name: 'http_addr', type: 'string', description: 'Address the web UI is served on', default: ':8002' },
        { name: 'max_input_size', type: 'number', description: 'Largest frame', default: 4194304 },
        { name: 'ice_servers', type: 'array', description: 'STUN/TURN servers', default: [] },
        { name: 'mapbox', type: 'string', description: 'Mapbox token', default: '' },
        { name: 'verbose', type: 'boolean', description: 'Log more' },
    ],
};

const packages = [{ uri: GCS_PKG, plugins: [gcsSpec, { name: 'broken', error: 'bad toml', args: null }] }];

// A form as the server sends it: every key present, zero values for unset.
function serverForm(overrides = {}) {
    return {
        daemon: { port: 9000, lcs: 9001, hostname: 'glide', cert: '', orchestrator: null, extra: { future: 1 } },
        simulator: { package: 'github.com/org/sim', args: null, extra: null },
        services: [
            {
                name: 'gcs', type: 'engine', auth: 'admin', simulate: false,
                plugin: { plugin: 'gcs', package: GCS_PKG, isolation: '', environment: 'cuda126', args: { http_addr: ':8002', mapbox: 'pk.test' }, extra: { pin: true } },
                extra: { weight: 2 },
            },
        ],
        extra: { extra_table: { a: 'b' } },
        ...overrides,
    };
}

describe('pluginSpec', () => {
    it('finds a plugin by package and name', () => {
        expect(pluginSpec(packages, GCS_PKG, 'gcs')).toBe(gcsSpec);
        expect(pluginSpec(packages, GCS_PKG, 'nope')).toBeNull();
        expect(pluginSpec(packages, 'other', 'gcs')).toBeNull();
    });
});

describe('argsState', () => {
    it('prefills declared args with their defaults', () => {
        const st = argsState({}, gcsSpec);
        const http = st.declared.find((d) => d.name === 'http_addr');
        expect(http).toMatchObject({ value: ':8002', present: false, description: 'Address the web UI is served on' });
        expect(st.declared.find((d) => d.name === 'verbose')).toMatchObject({ value: false, present: false });
        expect(st.rows).toEqual([]);
    });

    it('marks args in the config as present and keeps undeclared ones as rows', () => {
        const st = argsState({ http_addr: ':9000', extra_key: 'x' }, gcsSpec);
        expect(st.declared.find((d) => d.name === 'http_addr')).toMatchObject({ value: ':9000', present: true });
        expect(st.rows).toHaveLength(1);
        expect(st.rows[0]).toMatchObject({ key: 'extra_key', value: 'x' });
    });

    it('with no spec, or a spec that failed to parse, makes every arg a row', () => {
        expect(argsState({ a: 1 }, null)).toMatchObject({ declared: [], rows: [{ key: 'a', type: 'number', value: '1' }] });
        expect(argsState({ a: 1 }, { name: 'broken', error: 'bad', args: null }).declared).toEqual([]);
    });
});

describe('argsValue', () => {
    it('keeps args from the config even at their defaults', () => {
        const st = argsState({ http_addr: ':8002', mapbox: 'pk.test' }, gcsSpec);
        const mapbox = st.declared.find((d) => d.name === 'mapbox');
        mapbox.value = 'pk.other';
        expect(argsValue(st)).toEqual({ value: { http_addr: ':8002', mapbox: 'pk.other' }, errors: {} });
    });

    it('omits args not in the config that are still at their defaults', () => {
        expect(argsValue(argsState({}, gcsSpec))).toEqual({ value: {}, errors: {} });
    });

    it('writes args changed from their defaults', () => {
        const st = argsState({}, gcsSpec);
        st.declared.find((d) => d.name === 'max_input_size').value = 1024;
        st.declared.find((d) => d.name === 'verbose').value = true;
        expect(argsValue(st).value).toEqual({ max_input_size: 1024, verbose: true });
    });

    it('reads JSON-typed args from their text, and flags bad JSON', () => {
        const st = argsState({}, gcsSpec);
        const ice = st.declared.find((d) => d.name === 'ice_servers');
        expect(ice.text).toBe('[]');
        ice.text = '["stun:x"]';
        expect(argsValue(st).value).toEqual({ ice_servers: ['stun:x'] });
        ice.text = '[oops';
        expect(argsValue(st).errors.ice_servers).toBeTruthy();
        ice.text = '{"a": 1}';
        expect(argsValue(st).errors.ice_servers).toBeTruthy();
    });

    it('flags an undeclared row that repeats a declared arg', () => {
        const st = argsState({ extra_key: 'x' }, gcsSpec);
        st.rows[0].key = 'http_addr';
        expect(Object.keys(argsValue(st).errors)).toHaveLength(1);
    });
});

describe('formToState and stateToForm', () => {
    it('round-trips a form, extra keys included', () => {
        const form = serverForm();
        const { form: out, errors } = stateToForm(formToState(form, packages));
        expect(errors).toEqual({});
        expect(out).toEqual(form);
    });

    it('writes the pixi environment trimmed, and empty for the default', () => {
        const state = formToState(serverForm(), packages);
        expect(state.services[0].environment).toBe('cuda126');
        state.services[0].environment = '  cpu ';
        expect(stateToForm(state).form.services[0].plugin.environment).toBe('cpu');
        state.services[0].environment = '';
        expect(stateToForm(state).form.services[0].plugin.environment).toBe('');
    });

    it('validates names, types, plugins, packages and ports', () => {
        const state = formToState(serverForm(), packages);
        const [gcs] = state.services;
        state.services.push({ ...gcs, key: 'dup', origName: null });
        state.daemon.port = 70000;
        const { errors } = stateToForm(state);
        expect(errors['daemon.port']).toBeTruthy();
        expect(errors['services.dup.name']).toMatch(/Duplicate/);

        const s2 = formToState(serverForm(), packages);
        s2.services[0].name = '';
        s2.services[0].type = 'monitor';
        s2.services[0].plugin = '';
        s2.services[0].package = ' ';
        const key = s2.services[0].key;
        const e2 = stateToForm(s2).errors;
        expect(e2[`services.${key}.name`]).toBeTruthy();
        expect(e2[`services.${key}.type`]).toBeTruthy();
        expect(e2[`services.${key}.plugin`]).toBeTruthy();
        expect(e2[`services.${key}.package`]).toBeTruthy();
    });

    it('requires a plugin and package even for simulated services, as the daemon does', () => {
        const state = formToState(serverForm(), packages);
        state.services[0].simulate = true;
        state.services[0].plugin = '';
        expect(stateToForm(state).errors[`services.${state.services[0].key}.plugin`]).toBeTruthy();
    });

    it('lists the service types', () => {
        expect(SERVICE_TYPES).toEqual(['vehicle', 'engine', 'extra']);
    });
});

describe('guards', () => {
    const remote = { own: false, selfService: '', host: 'heisenberg' };
    const own = { own: true, selfService: 'gcs', host: 'glide' };

    it('asks to confirm removing or renaming the GCS on its own daemon', () => {
        const original = formToState(serverForm(), packages);
        const removed = formToState(serverForm({ services: [] }), packages);
        expect(guards(removed, original, own).confirmations).toEqual([
            'This removes or renames "gcs", the GCS you are using. It will not come back after the restart.',
        ]);
        const renamed = formToState(serverForm(), packages);
        renamed.services[0].name = 'gcs2';
        expect(guards(renamed, original, own).confirmations).toHaveLength(1);
        expect(guards(formToState(serverForm(), packages), original, own).confirmations).toEqual([]);
        expect(guards(removed, original, remote).confirmations).toEqual([]);
    });

    it("warns that a remote won't reconnect after an orchestrator or port change", () => {
        const original = formToState(serverForm(), packages);
        const changed = formToState(serverForm(), packages);
        changed.daemon.orchestrator = 'elsewhere:9000';
        changed.daemon.port = 9100;
        const { fieldWarnings } = guards(changed, original, remote);
        expect(fieldWarnings['daemon.orchestrator']).toBe("heisenberg won't reconnect to this GCS's daemon after this change.");
        expect(fieldWarnings['daemon.port']).toBe("heisenberg won't reconnect to this GCS's daemon after this change.");
    });

    it('warns of a hostname change, and of a port change on the own daemon', () => {
        const original = formToState(serverForm(), packages);
        const changed = formToState(serverForm(), packages);
        changed.daemon.hostname = 'renamed';
        changed.daemon.port = 9100;
        const hostname = "The daemon's network name, and possibly its IP, changes; this page may not find it again.";
        expect(guards(changed, original, remote).fieldWarnings['daemon.hostname']).toBe(hostname);
        const { fieldWarnings } = guards(changed, original, own);
        expect(fieldWarnings['daemon.hostname']).toBe(hostname);
        expect(fieldWarnings['daemon.port']).toBe("This GCS's daemon port changes; the GCS may not reconnect.");
        expect(guards(original, original, own).fieldWarnings).toEqual({});
    });
});

describe('restartWarning', () => {
    it('warns of the restart, and of the GCS on its own daemon', () => {
        const base = 'This restarts the daemon and every service on it, vehicles included. Do not do this while a vehicle is flying or running a mission.';
        expect(restartWarning({ own: false })).toBe(base);
        expect(restartWarning({ own: true })).toBe(`${base} This daemon also runs this GCS: the GCS restarts and this page disconnects until it is back.`);
    });
});

describe('applyOutcome', () => {
    const ok = { ok: true, conflict: false, errors: null, error: null, networkError: false };
    it('classifies the result of an apply or restart', () => {
        expect(applyOutcome(ok, { own: false })).toBe('applied');
        expect(applyOutcome({ ...ok, ok: false, networkError: true, error: 'Request failed' }, { own: true })).toBe('restarting');
        expect(applyOutcome({ ...ok, ok: false, networkError: true, error: 'Request failed' }, { own: false })).toBe('failed');
        expect(applyOutcome({ ...ok, ok: false, conflict: true, error: 'changed' }, { own: false })).toBe('conflict');
        expect(applyOutcome({ ...ok, ok: false, errors: ['e'], error: 'refused' }, { own: true })).toBe('refused');
        expect(applyOutcome({ ...ok, ok: false, error: 'denied' }, { own: false })).toBe('failed');
    });
});

describe('defaultValue', () => {
    it("is the arg's default, or its type's empty value", async () => {
        const { defaultValue } = await import('./configForm.js');
        expect(defaultValue({ type: 'string', default: ':8002' })).toBe(':8002');
        expect(defaultValue({ type: 'string' })).toBe('');
        expect(defaultValue({ type: 'boolean' })).toBe(false);
        expect(defaultValue({ type: 'array' })).toEqual([]);
        expect(defaultValue({ type: 'number' })).toBeNull();
    });
});

describe('followStep', () => {
    it('waits until the daemon has gone and come back up', async () => {
        const { followStep, FOLLOW_GRACE_MS, FOLLOW_TIMEOUT_MS } = await import('./configForm.js');
        const start = { startedAt: 0, sawDown: false };
        const up = { state: 'up' };
        // Still listed up right after Apply: not back yet
        expect(followStep(start, up, 1000)).toEqual({ outcome: 'waiting', sawDown: false });
        // Gone (unlisted, or the list failed), then up
        const gone = followStep(start, null, 1500);
        expect(gone).toEqual({ outcome: 'waiting', sawDown: true });
        expect(followStep({ ...start, ...gone }, up, 2000).outcome).toBe('back');
        expect(followStep(start, { state: 'unreachable', error: 'restarting' }, 1500)).toEqual({ outcome: 'waiting', sawDown: true });
        // Up past the grace period counts even if it was never seen down
        expect(followStep(start, up, FOLLOW_GRACE_MS).outcome).toBe('back');
        // Still listed failed right after a fix is applied: the old reason, not the outcome
        expect(followStep(start, { state: 'services_failed', error: 'old' }, 500)).toEqual({ outcome: 'waiting', sawDown: false });
        expect(followStep({ startedAt: 0, sawDown: true }, { state: 'services_failed', error: 'x' }, 1500).outcome).toBe('failed');
        expect(followStep(start, { state: 'services_failed', error: 'x' }, FOLLOW_GRACE_MS).outcome).toBe('failed');
        expect(followStep({ startedAt: 0, sawDown: true }, null, FOLLOW_TIMEOUT_MS + 1).outcome).toBe('timeout');
    });
});

describe('setPlugin', () => {
    it('keeps an unfinished free-form row', async () => {
        const { setPlugin } = await import('./configForm.js');
        const svc = formToState(serverForm(), packages).services[0];
        svc.args.rows.push({ id: 'r1', key: '', type: 'string', value: 'half' });
        const next = setPlugin(svc, packages, 'github.com/other', 'gcs');
        expect(next.args.rows.some((r) => r.id === 'r1' && r.value === 'half')).toBe(true);
    });

    it('keeps half-typed JSON in a declared arg the new plugin also declares', async () => {
        const { setPlugin } = await import('./configForm.js');
        const svc = formToState(serverForm(), packages).services[0];
        svc.args.declared.find((d) => d.name === 'ice_servers').text = '[oops';
        const next = setPlugin(svc, packages, GCS_PKG, 'gcs');
        expect(next.args.declared.find((d) => d.name === 'ice_servers').text).toBe('[oops');
    });

    it("turns args the new plugin doesn't declare into rows, keeping their values", async () => {
        const { setPlugin } = await import('./configForm.js');
        const svc = formToState(serverForm(), packages).services[0];
        const next = setPlugin(svc, packages, 'github.com/other', 'x');
        expect(next.args.declared).toEqual([]);
        expect(argsValue(next.args).value).toEqual({ http_addr: ':8002', mapbox: 'pk.test' });
    });

    it('folds a valid row into an arg the new plugin declares', async () => {
        const { setPlugin } = await import('./configForm.js');
        const svc = formToState({ ...serverForm(), services: [{ ...serverForm().services[0], plugin: { ...serverForm().services[0].plugin, package: 'github.com/other' } }] }, packages).services[0];
        expect(svc.args.declared).toEqual([]);
        const next = setPlugin(svc, packages, GCS_PKG, 'gcs');
        expect(next.args.declared.find((d) => d.name === 'mapbox')).toMatchObject({ value: 'pk.test', present: true });
        expect(next.args.rows).toEqual([]);
    });
});

describe('serviceKeyByName', () => {
    it("finds a service's card by its name in the loaded config", () => {
        const state = formToState(serverForm(), packages);
        expect(serviceKeyByName(state, 'gcs')).toBe(state.services[0].key);
    });

    it('finds nothing for a service the config lacks, or with no form', () => {
        expect(serviceKeyByName(formToState(serverForm(), packages), 'yolo')).toBeNull();
        expect(serviceKeyByName(null, 'gcs')).toBeNull();
    });
});
