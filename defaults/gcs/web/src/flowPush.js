// The Flow Builder's push of its flow to its vehicle (POST /api/flows/push),
// to test the flow without deploying a mission.
import { daemonRequest, describeFailure } from './daemonApi.js';
import { toTemplate } from './flowModel.js';
import { displayName } from './vehicleNames.js';

// The push button's {label, disabled, tooltip} for the flow, given the
// catalog (null until loaded) and the flow's validation.
export function pushButtonState(flow, catalog, validation) {
    const label = flow.vehicle ? `Push flow to ${displayName(flow.vehicle)}` : 'Push flow';
    let tooltip;
    if (!flow.vehicle) tooltip = 'Select a vehicle first';
    else if (!catalog) tooltip = 'Loading the flow catalog';
    else if (flow.nodes.length === 0) tooltip = 'Add nodes to the flow first';
    else if (validation.errors.length > 0) tooltip = 'Fix the errors below first';
    return { label, disabled: tooltip !== undefined, tooltip };
}

// Resolves to why the push failed, or null if the vehicle's flow was set;
// never rejects.
export async function pushFlow(flow, catalog) {
    let template;
    try {
        template = toTemplate(flow, catalog);
    } catch (e) {
        return e.message;
    }
    const result = await daemonRequest('POST', '/api/flows/push', { flow: template });
    if (result.status >= 400 && typeof result.body?.detail === 'string') return result.body.detail;
    return describeFailure(result);
}
