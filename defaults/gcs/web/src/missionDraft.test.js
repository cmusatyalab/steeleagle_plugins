import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
    MISSION_DRAFT_KEY, MISSION_DRAFT_VERSION, emptyMissionDraft,
    loadMissionDraft, saveMissionDraft, highestNodeNumber,
} from './missionDraft.js';

function memoryStorage() {
    const data = new Map();
    return {
        getItem: (k) => (data.has(k) ? data.get(k) : null),
        setItem: (k, v) => data.set(k, String(v)),
        removeItem: (k) => data.delete(k),
    };
}

beforeEach(() => {
    vi.stubGlobal('localStorage', memoryStorage());
});

afterEach(() => {
    vi.unstubAllGlobals();
});

const draft = {
    nodes: [{ id: 'node-3', type: 'taskNode', position: { x: 10, y: 20 },
              data: { type_name: 'Takeoff', instance_id: 'takeoff_1', params: { altitude: 10 }, isStart: true } }],
    edges: [{ id: 'e1', source: 'node-3', target: 'node-3', type: 'selfLoop', data: { eventId: 'done' }, label: 'done' }],
    eventInstances: [{ instance_id: 'done', type_name: 'Done', params: {} }],
    startNodeId: 'node-3',
    features: JSON.stringify({ type: 'FeatureCollection', features: [{ type: 'Feature', properties: { name: 'a' } }] }),
};

describe('missionDraft', () => {
    it('loads an empty draft when nothing is stored', () => {
        expect(loadMissionDraft()).toEqual(emptyMissionDraft());
    });

    it('round-trips a saved draft, positions included', () => {
        saveMissionDraft(draft);
        expect(loadMissionDraft()).toEqual(draft);
        expect(JSON.parse(localStorage.getItem(MISSION_DRAFT_KEY)).version).toBe(MISSION_DRAFT_VERSION);
    });

    it('treats corrupt, malformed or other-version storage as empty', () => {
        localStorage.setItem(MISSION_DRAFT_KEY, '{not json');
        expect(loadMissionDraft()).toEqual(emptyMissionDraft());
        localStorage.setItem(MISSION_DRAFT_KEY, JSON.stringify({ version: MISSION_DRAFT_VERSION, nodes: {} }));
        expect(loadMissionDraft()).toEqual(emptyMissionDraft());
        localStorage.setItem(MISSION_DRAFT_KEY, JSON.stringify({ ...draft, version: MISSION_DRAFT_VERSION + 1 }));
        expect(loadMissionDraft()).toEqual(emptyMissionDraft());
    });

    it('survives a storage that refuses writes', () => {
        localStorage.setItem = () => { throw new Error('QuotaExceededError'); };
        expect(() => saveMissionDraft(draft)).not.toThrow();
    });

    it('finds the highest node-N id, ignoring other ids', () => {
        expect(highestNodeNumber([])).toBe(0);
        expect(highestNodeNumber([{ id: 'node-2' }, { id: 'takeoff_1' }, { id: 'node-11' }, { id: 'node-x' }])).toBe(11);
    });
});
