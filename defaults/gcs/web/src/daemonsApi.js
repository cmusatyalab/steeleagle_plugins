// The Orchestrate page's view of the daemons: every daemon the GCS's own
// daemon knows of, with its services (GET /api/daemons); restarting a
// service or a whole daemon; a daemon's config and packages. Every function
// resolves, never rejects.
import { daemonRequest, describeFailure, query } from './daemonApi.js';

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

// Resolves to {config, error}: the daemon's config as GET /api/daemons/config
// gives it ({raw_toml, config_hash, form, form_error, own, self_service}).
export async function fetchConfig(ip) {
    const result = await daemonRequest('GET', `/api/daemons/config?${query({ ip })}`);
    const error = failure(result);
    return { config: error ? null : result.body, error };
}

// Resolves to {result, error}: {raw_toml} or {form}, for the dialog's view
// switch.
export async function convertConfig(body) {
    const result = await daemonRequest('POST', '/api/daemons/config/convert', body);
    const error = failure(result);
    return { result: error ? null : result.body, error };
}

// Resolves to {result, error}: {errors, warnings, blocking} for a candidate
// config ({form} or {raw_toml}).
export async function checkConfig(ip, payload) {
    const result = await daemonRequest('POST', '/api/daemons/config/check', { ip, ...payload });
    const error = failure(result);
    return { result: error ? null : result.body, error };
}

// Resolves to {ok, conflict, errors, error, networkError}. errors is the
// list a refused config came back with; networkError is whether no answer
// came at all (as when the GCS restarts with its own daemon).
export async function applyConfig(ip, configHash, payload) {
    const result = await daemonRequest('PUT', '/api/daemons/config', { ip, config_hash: configHash, ...payload });
    const error = failure(result);
    return {
        ok: !error,
        conflict: result.status === 409 && result.body?.conflict === true,
        errors: result.status === 422 && Array.isArray(result.body?.errors) ? result.body.errors : null,
        error,
        networkError: result.status === 0,
    };
}

// Resolves to {ok, error, networkError}.
export async function restartDaemon(ip) {
    const result = await daemonRequest('POST', '/api/daemons/restart', { ip });
    const error = failure(result);
    return { ok: !error, error, networkError: result.status === 0 };
}

// Resolves to {packages, error}: the daemon's installed packages, each with
// its plugins and their declared args.
export async function fetchPackages(ip) {
    const result = await daemonRequest('GET', `/api/daemons/packages?${query({ ip })}`);
    const error = failure(result);
    return { packages: error ? null : (result.body.packages ?? []), error };
}

// Resolves to why the install failed, or null. It takes effect when the
// daemon next restarts.
export async function installPackage(ip, uri) {
    return failure(await daemonRequest('POST', '/api/daemons/packages/install', { ip, uri }));
}

// Resolves to why the removal failed, or null. It takes effect when the
// daemon next restarts.
export async function removePackage(ip, uri) {
    return failure(await daemonRequest('POST', '/api/daemons/packages/remove', { ip, uri }));
}

// A daemon's name for people: its hostname, else its IP.
export function daemonName(daemon) {
    return daemon.hostname || daemon.ip;
}
