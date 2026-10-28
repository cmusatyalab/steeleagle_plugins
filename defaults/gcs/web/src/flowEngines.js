// Engine choices for mission fields of engine type (a task or event that
// names an engine of the flow, e.g. a DetectionEngine). The flow on the Flow
// Builder tab is the source of truth for which engines exist.
import { engineLocation, findTemplateEngine } from './flowCatalog.js';

const TYPED_ENGINES = new Set(['Detection', 'Classification', 'Guidance', 'Position', 'Generic']);

// The mission engine type for a catalog engine's result message type
// ("…/DetectionResult" -> "DetectionEngine"); anything else, or no result,
// is a GenericEngine.
export function engineTypeOf(result) {
    const last = (result ?? '').split(/[/.]/).pop();
    const match = /^(\w+)Result$/.exec(last);
    return match && TYPED_ENGINES.has(match[1]) ? `${match[1]}Engine` : 'GenericEngine';
}

// An engine's result message type: its `result` if the catalog gives one,
// else the type of its "result" event.
function resultType(engine) {
    return engine.result ?? engine.events?.find((e) => e.label === 'result')?.type ?? null;
}

// The flow's engines usable where an `engineType` is wanted, as dropdown
// options: label "<template name> (<local|remote>)", value the template name,
// which is what the graph stores.
export function engineChoices(flow, catalog, engineType) {
    if (!flow || !catalog) return [];
    const seen = new Set();
    const choices = [];
    for (const node of flow.nodes) {
        if (node.kind !== 'engine') continue;
        const engine = findTemplateEngine(catalog, node.engine);
        if (!engine || engineTypeOf(resultType(engine)) !== engineType) continue;
        const location = engineLocation(catalog, flow.vehicle, node.engine);
        if (location === null) continue;
        if (seen.has(node.engine)) continue;
        seen.add(node.engine);
        choices.push({ label: `${node.engine} (${location})`, value: node.engine });
    }
    return choices;
}
