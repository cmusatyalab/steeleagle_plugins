// The Orchestrate page's view of the daemons: every daemon the GCS's own
// daemon knows of, with its services (GET /api/daemons), and restarting a
// service on any of them (POST /api/daemons/restart_service).
import { daemonRequest, describeFailure } from './daemonApi.js';

// Why a request failed: the server's detail if it gave one, else the
// generic description; null if it succeeded.
function failure(result) {
    if (result.status >= 400 && typeof result.body?.detail === 'string') return result.body.detail;
    return describeFailure(result);
}

// Resolves to {daemons, error}; never rejects. On failure daemons is null,
// so a caller can keep showing the last list it had.
export async function fetchDaemons() {
    const result = await daemonRequest('GET', '/api/daemons');
    const error = failure(result);
    if (error) return { daemons: null, error };
    return { daemons: result.body.daemons ?? [], error: null };
}

// Resolves to why the restart failed, or null if it was done.
export async function restartService(name) {
    return failure(await daemonRequest('POST', '/api/daemons/restart_service', { name }));
}

// A daemon's name for people: its hostname, else its IP.
export function daemonName(daemon) {
    return daemon.hostname || daemon.ip;
}
