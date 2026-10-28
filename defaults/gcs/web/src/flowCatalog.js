// The Flow Builder's view of what the GCS can see: every daemon's vehicles
// (and their streams) and engines (and the types they accept and produce),
// from GET /api/flows/catalog. The GCS's own daemon (`local: true`) is the
// orchestrator, where remote engines run; `unavailable` lists daemons the
// GCS knows of but couldn't list. Names are full service names
// ("100.64.0.2.charlie"): vehicleNames.js splits them for display.
import { daemonRequest, describeFailure } from './daemonApi.js';
import { daemonOf, displayName, isIP } from './vehicleNames.js';

// Resolves to {daemons, unavailable, error}; never rejects. `error` is the
// server's reason (no daemon connection, a failed listing) or null.
export async function fetchCatalog() {
    const result = await daemonRequest('GET', '/api/flows/catalog');
    const detail = result.status >= 400 && typeof result.body?.detail === 'string' ? result.body.detail : null;
    const error = detail ?? describeFailure(result);
    if (error) return { daemons: [], unavailable: [], error };
    return { daemons: result.body.daemons ?? [], unavailable: result.body.unavailable ?? [], error: null };
}

// Refreshes can overlap (a Refresh click while a load is still running);
// only the most recent request's catalog is delivered. A failed refresh keeps
// the last good daemons and unavailable list (with the new error) rather than
// replacing them with nothing.
export function createCatalogLoader(onCatalog) {
    let latest = 0;
    let lastGood = null;
    return async () => {
        const request = ++latest;
        const catalog = await fetchCatalog();
        if (request !== latest) return;
        if (!catalog.error) lastGood = catalog;
        onCatalog(catalog.error && lastGood
            ? { daemons: lastGood.daemons, unavailable: lastGood.unavailable, error: catalog.error }
            : catalog);
    };
}

export function findDaemon(catalog, ip) {
    return catalog.daemons.find((d) => d.ip === ip) ?? null;
}

export function findVehicle(catalog, fullName) {
    if (!fullName) return null;
    for (const daemon of catalog.daemons) {
        const vehicle = daemon.vehicles.find((v) => v.name === fullName);
        if (vehicle) return vehicle;
    }
    return null;
}

export function findEngine(catalog, fullName) {
    if (!fullName) return null;
    for (const daemon of catalog.daemons) {
        const engine = daemon.engines.find((e) => e.name === fullName);
        if (engine) return engine;
    }
    return null;
}

// The GCS's own daemon, where remote engines run.
export function orchestrator(catalog) {
    return catalog.daemons.find((d) => d.local) ?? null;
}

// A daemon's name for people: its hostname, else its address.
export function daemonLabel(daemon) {
    return daemon.hostname || daemon.ip;
}

// A protobuf message type by its last path segment, as the UI shows it
// (steeleagle_protocol/v1/messages/frame/RawFrame -> RawFrame).
export function shortType(type) {
    if (type === null || type === undefined) return '—';
    return type.slice(type.lastIndexOf('/') + 1);
}

// Template names are "{host}.{name}": the host (an IP or a hostname, either
// of which may contain dots) is everything before the last dot. These mirror
// gcs/internal/flows/hosts.go, with identical error texts.
export function splitTemplateName(name) {
    if (typeof name !== 'string') return null;
    const i = name.lastIndexOf('.');
    if (i <= 0 || i === name.length - 1) return null;
    return { host: name.slice(0, i), bare: name.slice(i + 1) };
}

function firstLabel(h) {
    const i = h.indexOf('.');
    return i < 0 ? h : h.slice(0, i);
}

function sameName(a, b) {
    return a !== '' && a.toLowerCase() === b.toLowerCase();
}

// The daemon a template host names, using only the catalog: an IP literal
// names that daemon if listed; otherwise a hostname matches exactly, then by
// first label, ignoring case. `unavailable` entries are matched the same
// ways, to explain a host whose daemon couldn't be listed.
// Returns {daemon, error}; daemon is null on failure.
export function resolveHost(catalog, host) {
    const byIp = findDaemon(catalog, host);
    if (byIp) return { daemon: byIp, error: null };
    const isIp = isIP(host);
    for (const byLabel of [false, true]) {
        if (byLabel && isIp) break; // an IP has no short name
        const matches = catalog.daemons
            .filter((d) => sameName(byLabel ? firstLabel(d.hostname ?? '') : (d.hostname ?? ''), byLabel ? firstLabel(host) : host))
            .sort((a, b) => (a.ip < b.ip ? -1 : a.ip > b.ip ? 1 : 0));
        if (matches.length > 1) {
            return { daemon: null, error: `flow host ${host} is ambiguous: it matches ${matches[0].ip} and ${matches[1].ip}` };
        }
        if (matches.length === 1) return { daemon: matches[0], error: null };
    }
    const down = (catalog.unavailable ?? []).find((u) => u.ip === host
        || sameName(u.hostname ?? '', host)
        || (!isIp && sameName(firstLabel(u.hostname ?? ''), firstLabel(host))));
    if (down) return { daemon: null, error: `daemon ${down.hostname || down.ip} is unavailable: ${down.reason}` };
    return { daemon: null, error: `flow host ${host} is not a known daemon` };
}

// The name a daemon is shown and stored under in flows: its hostname if that
// resolves back to it and no unavailable daemon has the same hostname (exactly
// or by first label), else its address.
export function hostLabel(catalog, daemon) {
    const first = (h) => h.split('.')[0].toLowerCase();
    const clash = (catalog.unavailable ?? []).some((u) => u.hostname
        && (u.hostname.toLowerCase() === daemon.hostname?.toLowerCase() || first(u.hostname) === first(daemon.hostname ?? '')));
    if (daemon.hostname && !clash && resolveHost(catalog, daemon.hostname).daemon?.ip === daemon.ip) return daemon.hostname;
    return daemon.ip;
}

// A catalog service name ("100.64.0.2.yolo") as a template name
// ("swap03.yolo"); unchanged if its daemon isn't listed.
export function templateName(catalog, name) {
    const ip = daemonOf(name);
    const daemon = ip ? findDaemon(catalog, ip) : null;
    return daemon ? `${hostLabel(catalog, daemon)}.${displayName(name)}` : name;
}

// A template name as the catalog's service name ("{ip}.{bare}"), or the
// reason its host doesn't resolve. Returns {name, error}.
export function serviceName(catalog, template) {
    const parts = splitTemplateName(template);
    if (!parts) return { name: null, error: `flow name ${template} has no host` };
    const { daemon, error } = resolveHost(catalog, parts.host);
    if (!daemon) return { name: null, error };
    return { name: `${daemon.ip}.${parts.bare}`, error: null };
}

export function findTemplateEngine(catalog, template) {
    return findEngine(catalog, serviceName(catalog, template).name);
}

// Where an engine runs for a flow on `vehicleName`: 'local' (the vehicle's
// own daemon), 'remote' (the orchestrator), or null when it is on neither.
// Without a vehicle only the orchestrator is known, as 'remote'. The engine
// is a template name.
export function engineLocation(catalog, vehicleName, engineTemplate) {
    const parts = splitTemplateName(engineTemplate);
    const ip = parts ? resolveHost(catalog, parts.host).daemon?.ip : null;
    if (vehicleName && ip && ip === daemonOf(vehicleName)) return 'local';
    return ip && orchestrator(catalog)?.ip === ip ? 'remote' : null;
}

// Every resolution has these keys, resolved or not: `output` is a producer's
// stream type and `stream` its catalog entry; `accepts`, `events` (labels),
// `eventTypes` ({label: message type}) and `category` are an engine's.
const EMPTY = { output: null, stream: null, accepts: [], events: [], eventTypes: {}, category: null, typed: true, problem: null };

function unresolved(reason) {
    return { resolved: false, reason, ...EMPTY };
}

// Why a service name isn't in the catalog: its daemon couldn't be listed, or
// the daemon doesn't have it.
function missingReason(catalog, fullName, what) {
    const ip = daemonOf(fullName);
    const down = catalog.unavailable.find((u) => u.ip === ip);
    if (down) return `daemon ${down.hostname || down.ip} is unavailable: ${down.reason}`;
    return `${displayName(fullName)}: no such ${what}${ip ? ` on ${ip}` : ''}`;
}

// Producer nodes are the flow vehicle's {stream}; engine nodes' `engine` is a
// template name.
export function resolveNode(flow, catalog, node) {
    if (node.kind === 'producer') {
        if (!flow.vehicle) return unresolved('no vehicle selected');
        const vehicle = findVehicle(catalog, flow.vehicle);
        if (!vehicle) return unresolved(missingReason(catalog, flow.vehicle, 'vehicle'));
        const stream = vehicle.streams.find((s) => s.name === node.stream);
        if (!stream) return unresolved(`${displayName(flow.vehicle)} has no stream ${node.stream}`);
        return { resolved: true, reason: null, ...EMPTY, output: stream.type, stream, typed: vehicle.typed, problem: vehicle.problem ?? null };
    }
    const parts = splitTemplateName(node.engine);
    if (!parts) return unresolved(`flow name ${node.engine} has no host`);
    const { daemon, error } = resolveHost(catalog, parts.host);
    if (!daemon) return unresolved(error);
    const engine = findEngine(catalog, `${daemon.ip}.${parts.bare}`);
    if (!engine) return unresolved(`${node.engine}: no such engine on ${parts.host}`);
    // Engines have no output type: they forward the source data they receive
    // (flowModel.forwardedTypes).
    return {
        resolved: true,
        reason: null,
        ...EMPTY,
        accepts: engine.accepts,
        events: engine.events.map((e) => e.label),
        eventTypes: Object.fromEntries(engine.events.map((e) => [e.label, e.type])),
        category: engine.category ?? null,
        typed: engine.typed,
        problem: engine.problem ?? null,
        location: engineLocation(catalog, flow.vehicle, node.engine),
    };
}

// A vehicle's name, plus its daemon only when another vehicle in `among`
// has the same name on a different daemon.
export function vehicleLabel(fullName, among = []) {
    const clash = among.some((o) => o !== fullName && displayName(o) === displayName(fullName));
    return clash ? `${displayName(fullName)} @ ${daemonOf(fullName)}` : displayName(fullName);
}

// Every vehicle's full name, plus the current binding even if the catalog
// lacks it, so the picker never silently shows nothing.
export function vehicleOptions(catalog, current) {
    const options = catalog.daemons.flatMap((d) => d.vehicles.map((v) => v.name));
    if (current && !options.includes(current)) options.push(current);
    return options;
}
