// The flow the Flow Builder starts with: the GCS's default_flow arg, a flow
// config TOML named relative to its vehicle (vehicle.rgb, local.box_rerank,
// remote.detection), from GET /api/flows/default. Each version of the file
// is loaded once, replacing the Flow Builder's flow, so edits to it are kept
// until the file changes.
import { daemonRequest, describeFailure } from './daemonApi.js';
import { displayName } from './vehicleNames.js';

const VERSION_KEY = 'steeleagle-flow-default-version';

// Resolves to {configured, name, vehicle, toml, version}, or {configured:
// false, error} when it can't be had; never rejects.
export async function fetchDefaultFlow() {
    const result = await daemonRequest('GET', '/api/flows/default');
    const error = describeFailure(result);
    if (error) return { configured: false, error };
    return result.body;
}

// The full name of the vehicle the default flow is for, among the catalog's
// vehicles: `wanted` by its full or bare name; without one, `current` (the
// Flow Builder's vehicle) if it is running, else the first running vehicle.
// null if none is running yet.
export function defaultFlowVehicle(catalog, wanted, current) {
    const names = (catalog?.daemons ?? []).flatMap((d) => (d.vehicles ?? []).map((v) => v.name));
    if (wanted) return names.find((n) => n === wanted || displayName(n) === wanted) ?? null;
    if (current && names.includes(current)) return current;
    return names[0] ?? null;
}

// Whether `version` of the default flow has been loaded already.
export function defaultFlowLoaded(version) {
    try {
        return localStorage.getItem(VERSION_KEY) === version;
    } catch {
        return false;
    }
}

export function markDefaultFlowLoaded(version) {
    try {
        localStorage.setItem(VERSION_KEY, version);
    } catch {
        // storage blocked: it is loaded again on the next visit
    }
}
