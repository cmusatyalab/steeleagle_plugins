// Editor state for the simulator's vehicles, engines and actors (the
// simulator's args.vehicles, args.engines and args.actors arrays of tables).
//
// A simulated vehicle or engine service is played by the simulator entry of
// the same name, so that entry is edited on the service's card (svc.sim) and
// written from it. Entries no simulated service claims are orphans: kept as
// they are, and listed in the Simulator tab to remove or turn into a
// service. Actors have no service and are edited in the Simulator tab.
//
// Points are [lat, lon] or [lat, lon, alt] arrays, or {lat, lon, alt, ...}
// tables (steeleagle_aviary's parse_point). One the form can't show is kept
// as it is (originRaw, waypointsRaw).

import { fromRows, toRows } from './kvArgs.js';

// The simulator's list for each simulatable service type
export const SIM_LISTS = { vehicle: 'vehicles', engine: 'engines' };
// The interface the simulator uses when an entry doesn't name one
export const DEFAULT_INTERFACE = { vehicle: 'steeleagle', engine: 'detection' };
// The plugin type of each service type's plugins, in package listings
const PLUGIN_TYPES = { vehicle: 'driver', engine: 'engine' };
const LISTS = ['vehicles', 'engines', 'actors'];

let nextKey = 0;
const newKey = (prefix) => `${prefix}-${nextKey++}`;

const isNumber = (v) => typeof v === 'number' && Number.isFinite(v);
const isTable = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
const nilIfEmpty = (m) => (m && Object.keys(m).length ? m : null);

// A point as {lat, lon, alt, extra} (alt null if unset, extra the table's
// other keys), or null if the form can't show it.
export function parsePoint(p) {
    if (Array.isArray(p)) {
        if ((p.length === 2 || p.length === 3) && p.every(isNumber)) return { lat: p[0], lon: p[1], alt: p[2] ?? null, extra: null };
        return null;
    }
    if (isTable(p) && isNumber(p.lat) && isNumber(p.lon) && (p.alt === undefined || isNumber(p.alt))) {
        const { lat, lon, alt, ...extra } = p;
        return { lat, lon, alt: alt ?? null, extra: nilIfEmpty(extra) };
    }
    return null;
}

export const emptyPoint = () => ({ lat: null, lon: null, alt: null });

// The error for a point, or null. Altitude is optional (the simulator
// takes 0).
export function pointError(pt) {
    if (pt.lat === null || pt.lon === null) return 'Latitude and longitude are required';
    if (pt.lat < -90 || pt.lat > 90) return 'Latitude is from -90 to 90';
    if (pt.lon < -180 || pt.lon > 180) return 'Longitude is from -180 to 180';
    return null;
}

const pointArray = (pt) => (pt.alt === null ? [pt.lat, pt.lon] : [pt.lat, pt.lon, pt.alt]);

// Takes an entry's origin out of rest: its origin key, or its own lat, lon
// and alt keys. Returns {origin, originRaw}; originRaw is an origin key the
// form can't show, which stays as it is.
function takeOrigin(rest) {
    if ('origin' in rest) {
        const pt = Array.isArray(rest.origin) ? parsePoint(rest.origin) : null;
        if (!pt) return { origin: emptyPoint(), originRaw: rest.origin };
        delete rest.origin;
        return { origin: { lat: pt.lat, lon: pt.lon, alt: pt.alt }, originRaw: undefined };
    }
    if (isNumber(rest.lat) && isNumber(rest.lon) && (rest.alt === undefined || isNumber(rest.alt))) {
        const origin = { lat: rest.lat, lon: rest.lon, alt: rest.alt ?? null };
        delete rest.lat;
        delete rest.lon;
        delete rest.alt;
        return { origin, originRaw: undefined };
    }
    return { origin: emptyPoint(), originRaw: undefined };
}

// Takes an entry's args table out of rest, as rows. args that isn't a table
// stays in rest.
function takeArgs(rest) {
    if (!isTable(rest.args)) return { argRows: [], hadArgs: false };
    const argRows = toRows(rest.args);
    delete rest.args;
    return { argRows, hadArgs: true };
}

// The editor state of a vehicle or engine entry (its name is the
// service's).
export function entryState(entry = {}) {
    const rest = { ...entry };
    delete rest.name;
    const iface = typeof rest.interface === 'string' ? rest.interface : '';
    if (typeof rest.interface === 'string') delete rest.interface;
    const { origin, originRaw } = takeOrigin(rest);
    const { argRows, hadArgs } = takeArgs(rest);
    return { interface: iface, origin, originRaw, argRows, hadArgs, extra: nilIfEmpty(rest) };
}

function waypointState(p) {
    const pt = parsePoint(p);
    const extra = { ...(pt.extra ?? {}) };
    const speed = isNumber(extra.speed) ? extra.speed : null;
    const delay = isNumber(extra.delay) ? extra.delay : null;
    if (speed !== null) delete extra.speed;
    if (delay !== null) delete extra.delay;
    return { key: newKey('wp'), lat: pt.lat, lon: pt.lon, alt: pt.alt, speed, delay, extra: nilIfEmpty(extra) };
}

export function newWaypoint() {
    return { key: newKey('wp'), ...emptyPoint(), speed: null, delay: null, extra: null };
}

export function actorState(entry = {}) {
    const rest = { ...entry };
    const tag = typeof rest.tag === 'string' ? rest.tag : '';
    if (typeof rest.tag === 'string') delete rest.tag;
    const name = typeof rest.name === 'string' ? rest.name : '';
    if (typeof rest.name === 'string') delete rest.name;
    const { origin, originRaw } = takeOrigin(rest);
    let waypoints = [];
    let waypointsRaw;
    if ('waypoints' in rest) {
        if (Array.isArray(rest.waypoints) && rest.waypoints.every((w) => parsePoint(w))) waypoints = rest.waypoints.map(waypointState);
        else waypointsRaw = rest.waypoints;
        delete rest.waypoints;
    }
    const { argRows, hadArgs } = takeArgs(rest);
    return { key: newKey('actor'), tag, name, origin, originRaw, waypoints, waypointsRaw, argRows, hadArgs, extra: nilIfEmpty(rest) };
}

// The service type a simulator list holds
const kindOf = (list) => (list === 'vehicles' ? 'vehicle' : 'engine');

// Splits the simulator's args into the form's parts, claiming for each
// simulated vehicle and engine service its entry. Returns
// {rest, actors, orphans, lists, claimed}: rest is the args left for the
// key/value rows, lists the lists the args had (written back even if they
// end up empty), claimed maps a service's index to its entry state.
export function splitSimulatorArgs(args, services) {
    const rest = { ...(args ?? {}) };
    const lists = {};
    for (const list of LISTS) {
        if (Array.isArray(rest[list]) && rest[list].every(isTable)) {
            lists[list] = rest[list];
            delete rest[list];
        }
    }
    const pools = { vehicles: [...(lists.vehicles ?? [])], engines: [...(lists.engines ?? [])] };
    const claimed = {};
    services.forEach((s, i) => {
        const list = SIM_LISTS[s.type];
        if (!s.simulate || !list) return;
        const at = pools[list].findIndex((e) => e.name === s.name);
        if (at < 0) return;
        claimed[i] = entryState(pools[list][at]);
        pools[list].splice(at, 1);
    });
    const orphans = ['vehicles', 'engines'].flatMap((list) => pools[list].map((entry) => ({ key: newKey('orphan'), kind: kindOf(list), entry })));
    return {
        rest,
        actors: (lists.actors ?? []).map(actorState),
        orphans,
        lists: Object.fromEntries(Object.keys(lists).map((l) => [l, true])),
        claimed,
    };
}

function writeArgs(out, rows, hadArgs, errors, field) {
    const args = fromRows(rows);
    if (Object.keys(args.errors).length) errors[field] = args.errors;
    if (hadArgs || rows.length) out.args = args.value;
}

// The simulator entry of a simulated service, and its errors by field
// (origin, args).
export function entryValue(svc) {
    const errors = {};
    const out = { ...(svc.sim.extra ?? {}), name: svc.name.trim() };
    if (svc.sim.interface.trim()) out.interface = svc.sim.interface.trim();
    if (svc.type === 'vehicle') {
        if (svc.sim.originRaw !== undefined) out.origin = svc.sim.originRaw;
        else {
            const err = pointError(svc.sim.origin);
            if (err) errors.origin = err;
            else out.origin = pointArray(svc.sim.origin);
        }
    }
    writeArgs(out, svc.sim.argRows, svc.sim.hadArgs, errors, 'args');
    return { value: out, errors };
}

function waypointValue(w) {
    const pt = pointArray(w);
    if (w.speed === null && w.delay === null && !w.extra) return pt;
    const out = { ...(w.extra ?? {}), lat: w.lat, lon: w.lon };
    if (w.alt !== null) out.alt = w.alt;
    if (w.speed !== null) out.speed = w.speed;
    if (w.delay !== null) out.delay = w.delay;
    return out;
}

// An actor's entry, and its errors by field (tag, origin, waypoints, args).
export function actorValue(a) {
    const errors = {};
    const out = { ...(a.extra ?? {}) };
    if (!a.tag.trim()) errors.tag = 'Required';
    out.tag = a.tag.trim();
    if (a.name.trim()) out.name = a.name.trim();
    if (a.originRaw !== undefined) out.origin = a.originRaw;
    else {
        const err = pointError(a.origin);
        if (err) errors.origin = err;
        else out.origin = pointArray(a.origin);
    }
    if (a.waypointsRaw !== undefined) out.waypoints = a.waypointsRaw;
    else if (a.waypoints.length) {
        const wpErrors = {};
        for (const w of a.waypoints) {
            const err = pointError(w);
            if (err) wpErrors[w.key] = err;
            else if ((w.speed !== null && w.speed <= 0) || (w.delay !== null && w.delay < 0)) wpErrors[w.key] = 'Speed must be positive and delay not negative';
        }
        if (Object.keys(wpErrors).length) errors.waypoints = wpErrors;
        out.waypoints = a.waypoints.map(waypointValue);
    }
    writeArgs(out, a.argRows, a.hadArgs, errors, 'args');
    return { value: out, errors };
}

export function newActor() {
    return actorState({});
}

// The service an orphan entry becomes: simulated, its plugin prefilled from
// the simulator's package.
export function orphanService(orphan, base, simPackage, packages) {
    return prefillPlugin({ ...base, name: orphan.entry.name ?? '', type: orphan.kind, simulate: true, sim: entryState(orphan.entry) }, simPackage, packages);
}

// A simulated service still needs a plugin, which the simulator package
// provides: if the service has none, its package is set to the simulator's
// and, if the package has exactly one plugin of the service's type, its
// plugin to that.
export function prefillPlugin(svc, simPackage, packages) {
    if (svc.plugin.trim() || svc.package.trim() || !simPackage.trim()) return svc;
    const pkg = (packages ?? []).find((p) => p.uri === simPackage.trim());
    const fits = (pkg?.plugins ?? []).filter((p) => p.type === PLUGIN_TYPES[svc.type]);
    return { ...svc, package: simPackage.trim(), plugin: fits.length === 1 ? fits[0].name : '' };
}
