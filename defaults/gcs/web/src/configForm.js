// Editor state for the Configure dialog, converted to and from the server's
// form model (GET /api/daemons/config, Form in internal/daemons/form.go):
//
//   { daemon: { port, lcs, hostname, cert, orchestrator, extra },  // null = not set
//     simulator: { package, args, extra } | null,
//     services: [{ name, type, auth, simulate,
//                  plugin: { plugin, package, isolation, environment, args, extra }, extra }],
//     extra }
//
// The `extra` maps hold the keys the form doesn't know; they pass through
// untouched. A service's args are shown from its plugin's TOML (the
// daemon's package list): each declared arg is prefilled with its default,
// and args the plugin doesn't declare are free-form rows. The simulator's
// vehicles, engines and actors are split out of its args (simForm.js): a
// simulated service's entry is edited as its sim.

import { fromRows, toRows } from './kvArgs.js';
import { SIM_LISTS, actorValue, entryState, entryValue, splitSimulatorArgs } from './simForm.js';

export const SERVICE_TYPES = ['vehicle', 'engine', 'extra'];
// '' is the daemon's default: privileged for vehicles, external otherwise
export const AUTH_LEVELS = ['', 'admin', 'privileged', 'user', 'external', 'unknown'];
export const JSON_TYPES = ['array', 'object'];

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const NAME_MAX_LENGTH = 64;

let nextKey = 0;
const serviceKey = () => `svc-${nextKey++}`;

export function validateName(value) {
    const name = value.trim();
    if (!name) return 'Required';
    if (name.length > NAME_MAX_LENGTH) return `At most ${NAME_MAX_LENGTH} characters`;
    if (!NAME_PATTERN.test(name)) return 'Letters, digits, ".", "_" and "-" only; must start with a letter or digit';
    return null;
}

function deepEqual(a, b) {
    if (a === b) return true;
    if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => deepEqual(a[k], b[k]));
}

const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));

// The value an arg with no default starts at.
function emptyFor(type) {
    switch (type) {
        case 'string': return '';
        case 'boolean': return false;
        case 'array': return [];
        case 'object': return {};
        default: return null;
    }
}

// The value a declared arg starts at, and returns to on reset: its default,
// or its type's empty value.
export function defaultValue(arg) {
    return clone(arg.default ?? emptyFor(arg.type));
}

// The plugin entry for a package and plugin name, or null.
export function pluginSpec(packages, uri, name) {
    const pkg = (packages ?? []).find((p) => p.uri === uri);
    return pkg?.plugins?.find((p) => p.name === name) ?? null;
}

// Editor state for a service's args, given the plugin's spec:
//   { declared: [{ name, type, description, default, present, value, text }],
//     rows: [kvArgs row] }
// present is whether the config sets the arg; text holds JSON-typed values
// while they are edited. With no usable spec, every arg is a row.
export function argsState(args, spec) {
    const values = args ?? {};
    const declaredSpecs = spec && !spec.error && Array.isArray(spec.args) ? spec.args : [];
    const declared = declaredSpecs.map((a) => {
        const present = Object.prototype.hasOwnProperty.call(values, a.name);
        const value = present ? clone(values[a.name]) : defaultValue(a);
        const entry = { name: a.name, type: a.type, description: a.description ?? '', default: a.default, present, value };
        if (JSON_TYPES.includes(a.type)) entry.text = JSON.stringify(value);
        return entry;
    });
    const names = new Set(declared.map((d) => d.name));
    const rest = Object.fromEntries(Object.entries(values).filter(([k]) => !names.has(k)));
    return { declared, rows: toRows(rest) };
}

// Returns { value, errors }: the args to write, and errors by arg name (or
// row id for free-form rows). A declared arg is written if the config set it,
// or if it now differs from its default.
export function argsValue(state) {
    const value = {};
    const errors = {};
    for (const d of state.declared) {
        let v = d.value;
        if (JSON_TYPES.includes(d.type)) {
            try {
                v = JSON.parse(d.text);
            } catch {
                errors[d.name] = 'Not valid JSON';
                continue;
            }
            const isArray = Array.isArray(v);
            if (d.type === 'array' ? !isArray : (isArray || typeof v !== 'object' || v === null)) {
                errors[d.name] = d.type === 'array' ? 'Must be a JSON array' : 'Must be a JSON object';
                continue;
            }
        }
        if (d.present || !deepEqual(v, d.default ?? emptyFor(d.type))) value[d.name] = v;
    }
    const rows = fromRows(state.rows);
    Object.assign(errors, rows.errors);
    const names = new Set(state.declared.map((d) => d.name));
    for (const row of state.rows) {
        if (names.has(row.key.trim()) && !errors[row.id]) errors[row.id] = `"${row.key.trim()}" is one of the plugin's args`;
    }
    for (const [k, v] of Object.entries(rows.value)) {
        if (!names.has(k)) value[k] = v;
    }
    return { value, errors };
}

function serviceState(s, packages, sim) {
    const plugin = s.plugin ?? {};
    return {
        key: serviceKey(),
        origName: s.name,
        name: s.name ?? '',
        type: s.type ?? '',
        auth: s.auth ?? '',
        simulate: !!s.simulate,
        // Kept while unsimulated, so unticking Simulated loses nothing
        sim: sim ?? entryState(),
        plugin: plugin.plugin ?? '',
        package: plugin.package ?? '',
        isolation: plugin.isolation ?? '',
        environment: plugin.environment ?? '',
        args: argsState(plugin.args, pluginSpec(packages, plugin.package, plugin.plugin)),
        hadArgs: plugin.args != null,
        extra: s.extra ?? null,
        pluginExtra: plugin.extra ?? null,
    };
}

export function newService() {
    return serviceState({ name: '', type: 'engine', plugin: {} }, []);
}

export function duplicateService(svc) {
    return { ...clone(svc), key: serviceKey(), origName: null, name: '' };
}

// The service with its plugin set to `name` from package `uri`, its args
// re-read against that plugin's spec. Nothing typed so far is lost, even
// what doesn't validate yet: an arg both plugins declare keeps its entry, a
// valid row naming an arg the new plugin declares becomes that arg, and
// everything else is kept as a row.
export function setPlugin(svc, packages, uri, name) {
    const spec = pluginSpec(packages, uri, name);
    const fresh = argsState({}, spec);
    const old = new Map(svc.args.declared.map((d) => [d.name, d]));
    const rows = [];
    const declared = fresh.declared.map((d) => {
        const prev = old.get(d.name);
        if (prev && prev.type === d.type) return { ...prev, description: d.description, default: d.default };
        return d;
    });
    const byName = new Map(declared.map((d, i) => [d.name, i]));
    for (const prev of svc.args.declared) {
        // Carried over above
        const i = byName.get(prev.name);
        if (i !== undefined && declared[i].type === prev.type) continue;
        // An arg the new plugin doesn't declare (or declares with another
        // type) is kept as a row if it was set or changed
        let value = prev.value;
        if (JSON_TYPES.includes(prev.type)) {
            try {
                value = JSON.parse(prev.text);
            } catch {
                value = prev.text;
            }
        }
        if (prev.present || !deepEqual(value, prev.default ?? emptyFor(prev.type))) rows.push(...toRows({ [prev.name]: value }));
    }
    for (const row of svc.args.rows) {
        const i = byName.get(row.key.trim());
        const parsed = i !== undefined ? fromRows([row]) : null;
        if (parsed && !Object.keys(parsed.errors).length) {
            const d = declared[i];
            const value = parsed.value[row.key.trim()];
            declared[i] = { ...d, present: true, value, ...(JSON_TYPES.includes(d.type) ? { text: JSON.stringify(value) } : {}) };
        } else rows.push(row);
    }
    return { ...svc, plugin: name, package: uri, args: { declared, rows }, hadArgs: true };
}

export function formToState(form, packages) {
    const daemon = form.daemon ?? {};
    const sim = form.simulator;
    const split = splitSimulatorArgs(sim?.args, form.services ?? []);
    return {
        daemon: {
            port: daemon.port ?? null,
            lcs: daemon.lcs ?? null,
            hostname: daemon.hostname ?? null,
            cert: daemon.cert ?? null,
            orchestrator: daemon.orchestrator ?? null,
        },
        daemonExtra: daemon.extra ?? null,
        simulator: {
            present: !!sim,
            package: sim?.package ?? '',
            argRows: toRows(split.rest),
            hadArgs: sim?.args != null,
            extra: sim?.extra ?? null,
            actors: split.actors,
            orphans: split.orphans,
            lists: split.lists,
        },
        services: (form.services ?? []).map((s, i) => serviceState(s, packages, split.claimed[i])),
        extra: form.extra ?? null,
    };
}

const text = (v) => (v === null ? null : v.trim());
const validPort = (v) => v === null || (Number.isInteger(v) && v >= 1 && v <= 65535);

// Returns { form, errors }. errors maps a field path ("daemon.port",
// "services.<key>.name", "services.<key>.args" → { arg: message }) to its
// message, and is empty when the form can be sent.
export function stateToForm(state) {
    const errors = {};
    const d = state.daemon;
    for (const key of ['port', 'lcs']) {
        if (!validPort(d[key])) errors[`daemon.${key}`] = 'A port from 1 to 65535';
    }
    const daemon = { port: d.port, lcs: d.lcs, hostname: text(d.hostname), cert: text(d.cert), orchestrator: text(d.orchestrator), extra: state.daemonExtra };

    // The simulator entries of the simulated services, by list
    const simEntries = { vehicles: [], engines: [] };
    // The daemon wants names unique across all services, whatever their type
    const seen = new Set();
    const services = state.services.map((s) => {
        const at = (field) => `services.${s.key}.${field}`;
        const name = s.name.trim();
        const nameError = validateName(s.name);
        if (nameError) errors[at('name')] = nameError;
        else if (seen.has(name)) errors[at('name')] = `Duplicate service name "${name}"`;
        seen.add(name);
        if (!SERVICE_TYPES.includes(s.type)) errors[at('type')] = `One of ${SERVICE_TYPES.join(', ')}`;
        if (!s.plugin.trim()) errors[at('plugin')] = 'Required';
        if (!s.package.trim()) errors[at('package')] = 'Required';
        const args = argsValue(s.args);
        if (Object.keys(args.errors).length) errors[at('args')] = args.errors;
        if (s.simulate) {
            if (!state.simulator.present) errors[at('simulate')] = 'Needs a simulator (Simulator tab)';
            else if (!SIM_LISTS[s.type]) errors[at('simulate')] = 'Only vehicles and engines can be simulated';
            else {
                const entry = entryValue(s);
                if (Object.keys(entry.errors).length) errors[at('sim')] = entry.errors;
                simEntries[SIM_LISTS[s.type]].push(entry.value);
            }
        }
        return {
            name,
            type: s.type,
            auth: s.auth,
            simulate: s.simulate,
            plugin: {
                plugin: s.plugin.trim(),
                package: s.package.trim(),
                isolation: s.isolation.trim(),
                environment: s.environment.trim(),
                args: s.hadArgs || Object.keys(args.value).length ? args.value : null,
                extra: s.pluginExtra,
            },
            extra: s.extra,
        };
    });
    let simulator = null;
    if (state.simulator.present) {
        const sim = state.simulator;
        const args = fromRows(sim.argRows);
        const argErrors = { ...args.errors };
        for (const row of sim.argRows) {
            const key = row.key.trim();
            if (['vehicles', 'engines', 'actors'].includes(key) && !argErrors[row.id]) argErrors[row.id] = `"${key}" is edited above`;
        }
        if (Object.keys(argErrors).length) errors['simulator.args'] = argErrors;
        const value = { ...args.value };
        for (const o of sim.orphans) simEntries[SIM_LISTS[o.kind]].push(o.entry);
        for (const [list, entries] of Object.entries(simEntries)) {
            if (entries.length || sim.lists[list]) value[list] = entries;
        }
        const actorErrors = {};
        const actors = sim.actors.map((a) => {
            const actor = actorValue(a);
            if (Object.keys(actor.errors).length) actorErrors[a.key] = actor.errors;
            return actor.value;
        });
        if (Object.keys(actorErrors).length) errors['simulator.actors'] = actorErrors;
        if (actors.length || sim.lists.actors) value.actors = actors;
        simulator = {
            package: sim.package.trim(),
            args: sim.hadArgs || Object.keys(value).length ? value : null,
            extra: sim.extra,
        };
    }
    return { form: { daemon, simulator, services, extra: state.extra }, errors };
}

export function hasErrors(errors) {
    return Object.keys(errors).length > 0;
}

// What a change may cost this GCS, compared with the config as loaded:
// confirmations the operator must tick before Apply, and warnings by field
// path. own: the daemon runs this GCS (selfService, its bare name); host is
// the daemon's name for people.
export function guards(state, original, { own, selfService, host }) {
    const confirmations = [];
    if (own && selfService && original.services.some((s) => s.name === selfService)) {
        const kept = state.services.some((s) => s.origName === selfService && s.name.trim() === selfService);
        if (!kept) confirmations.push(`This removes or renames "${selfService}", the GCS you are using. It will not come back after the restart.`);
    }
    const fieldWarnings = {};
    const changed = (key) => state.daemon[key] !== original.daemon[key];
    const reconnect = `${host} won't reconnect to this GCS's daemon after this change.`;
    if (own) {
        if (changed('port')) fieldWarnings['daemon.port'] = "This GCS's daemon port changes; the GCS may not reconnect.";
    } else {
        if (changed('orchestrator')) fieldWarnings['daemon.orchestrator'] = reconnect;
        if (changed('port')) fieldWarnings['daemon.port'] = reconnect;
    }
    if (changed('hostname')) fieldWarnings['daemon.hostname'] = "The daemon's network name, and possibly its IP, changes; this page may not find it again.";
    return { confirmations, fieldWarnings };
}

// The warning before anything that restarts a daemon.
export function restartWarning({ own }) {
    const base = 'This restarts the daemon and every service on it, vehicles included. Do not do this while a vehicle is flying or running a mission.';
    return own ? `${base} This daemon also runs this GCS: the GCS restarts and this page disconnects until it is back.` : base;
}

// What an apply or restart result means. On the GCS's own daemon the GCS
// restarts too, which can cut the answer off: that is a restart, not a
// failure.
export function applyOutcome(result, { own }) {
    if (result.ok) return 'applied';
    if (result.conflict) return 'conflict';
    if (result.errors) return 'refused';
    if (result.networkError && own) return 'restarting';
    return 'failed';
}

// How long after an apply or restart a daemon listed up counts as back even
// if the page never saw it go, and how long to wait for it at all.
export const FOLLOW_GRACE_MS = 5000;
export const FOLLOW_TIMEOUT_MS = 60000;

// One step of following a daemon after an apply or restart, given how it is
// listed now (null if it isn't, or the list failed): {outcome, sawDown},
// outcome 'waiting', 'back', 'failed' (its services didn't start) or
// 'timeout'.
export function followStep({ startedAt, sawDown }, daemon, now) {
    // Right after an apply the daemon can still be listed as it was, failed
    // services included, so a state only counts once it has gone and come
    // back, or after the grace period
    const settled = sawDown || now - startedAt >= FOLLOW_GRACE_MS;
    if (daemon?.state === 'services_failed' && settled) return { outcome: 'failed', sawDown };
    if (daemon?.state === 'up' && settled) return { outcome: 'back', sawDown };
    const down = sawDown || (daemon?.state !== 'up' && daemon?.state !== 'services_failed');
    if (now - startedAt > FOLLOW_TIMEOUT_MS) return { outcome: 'timeout', sawDown: down };
    return { outcome: 'waiting', sawDown: down };
}
