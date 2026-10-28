import { describe, it, expect } from 'vitest';
import { engineChoices, engineTypeOf } from './flowEngines.js';
import { GCS, SWAP, HARPY, catalogFixture } from './flowFixtures.js';
import { emptyFlow } from './flowModel.js';

const flow = () => ({
    ...emptyFlow(),
    vehicle: HARPY,
    nodes: [
        { id: 'e1', kind: 'engine', engine: `${SWAP.ip}.yolo`, position: { x: 0, y: 0 } },
        { id: 'e2', kind: 'engine', engine: `${GCS.ip}.telemetry`, position: { x: 0, y: 0 } },
    ],
});

describe('engineTypeOf', () => {
    it('maps a result type to its engine type, defaulting to generic', () => {
        expect(engineTypeOf('steeleagle_protocol/v1/messages/result/DetectionResult')).toBe('DetectionEngine');
        expect(engineTypeOf('x/ClassificationResult')).toBe('ClassificationEngine');
        expect(engineTypeOf('x/WeirdResult')).toBe('GenericEngine');
        expect(engineTypeOf(null)).toBe('GenericEngine');
    });
});

describe('engineChoices', () => {
    it('offers local detection engines for DetectionEngine', () => {
        expect(engineChoices(flow(), catalogFixture(), 'DetectionEngine')).toEqual([{ label: '100.64.0.2.yolo (local)', value: '100.64.0.2.yolo' }]);
    });

    it('labels and values engines by the template name the node stores', () => {
        const byName = { ...flow(), nodes: flow().nodes.map((n) => ({ ...n, engine: n.engine.replace(SWAP.ip, 'swap').replace(GCS.ip, 'gcs') })) };
        expect(engineChoices(byName, catalogFixture(), 'DetectionEngine')).toEqual([{ label: 'swap.yolo (local)', value: 'swap.yolo' }]);
        expect(engineChoices(byName, catalogFixture(), 'GenericEngine')).toEqual([{ label: 'gcs.telemetry (remote)', value: 'gcs.telemetry' }]);
    });

    it('offers remote engines for GenericEngine', () => {
        expect(engineChoices(flow(), catalogFixture(), 'GenericEngine')).toEqual([
            { label: `${GCS.ip}.telemetry (remote)`, value: `${GCS.ip}.telemetry` },
        ]);
    });

    it('honours an explicit catalog result and is empty for an empty flow or no catalog', () => {
        const catalog = catalogFixture();
        catalog.daemons[1].engines.find((e) => e.name.endsWith('.yolo')).result = 'x/GuidanceResult';
        expect(engineChoices(flow(), catalog, 'GuidanceEngine')).toHaveLength(1);
        expect(engineChoices(emptyFlow(), catalogFixture(), 'DetectionEngine')).toEqual([]);
        expect(engineChoices(flow(), null, 'DetectionEngine')).toEqual([]);
    });
});
