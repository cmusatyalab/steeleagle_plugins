// Shared test fixtures for the Flow Builder modules: two mock daemons
// running the seed's vehicles and engines, and the reference "Flow B, early
// discard" flow from docs/superpowers/specs/2026-10-16-gcs-flow-builder-design.md.

// The orchestrator (the GCS's own daemon), the vehicle's daemon, and a third
// daemon whose engines are reachable from neither.
export const GCS = { ip: '100.64.0.1', hostname: 'gcs', local: true };
export const SWAP = { ip: '100.64.0.2', hostname: 'swap', local: false };
export const LAB = { ip: '100.64.0.3', hostname: 'lab', local: false };
export const HARPY = `${SWAP.ip}.harpy`;

const M = 'steeleagle_protocol/v1/messages';
export const TYPES = {
    telemetry: `${M}/telemetry/Telemetry`,
    raw: `${M}/frame/RawFrame`,
    encoded: `${M}/frame/EncodedFrame`,
    detection: `${M}/result/DetectionResult`,
    generic: `${M}/result/GenericResult`,
};

// Mirrors the mock plugin files in GET /api/flows/catalog's shape.
const TELEMETRY = { name: 'telemetry', kind: 'telemetry', type: TYPES.telemetry, primary: false, description: null, hres: null, vres: null, fps: null };
const camera = (name, type, primary, description, hres, vres, fps) => ({ name, kind: 'camera', type, primary, description, hres, vres, fps });

const HARPY_STREAMS = [
    TELEMETRY,
    camera('frames', TYPES.raw, true, 'Main RGB camera on gimbal', 1080, 720, 30),
    camera('thermal', TYPES.raw, true, 'Thermal camera on gimbal', 640, 512, 30),
    camera('downward', TYPES.encoded, false, 'Downward-facing navigation camera', 640, 480, 15),
];
const KESTREL_STREAMS = [TELEMETRY, camera('rgb', TYPES.raw, true, 'Main RGB camera on gimbal', 1080, 720, 30)];

const DETECTOR_EVENTS = [{ label: 'result', type: TYPES.detection }, { label: 'dropped', type: TYPES.generic }];
const ENGINES = [
    { name: 'detector', category: 'detection', accepts: [TYPES.raw], events: DETECTOR_EVENTS, typed: true, problem: null },
    { name: 'yolo', category: 'detection', accepts: [TYPES.raw], events: DETECTOR_EVENTS, typed: true, problem: null },
    { name: 'telemetry', category: 'generic', accepts: [TYPES.telemetry, TYPES.raw], events: [{ label: 'result', type: TYPES.generic }], typed: true, problem: null },
];

const full = (daemon, name) => `${daemon.ip}.${name}`;
const engines = (daemon) => ENGINES.map((e) => ({ ...e, name: full(daemon, e.name) }));

export function catalogFixture() {
    return {
        daemons: [
            { ...GCS, vehicles: [], engines: engines(GCS) },
            {
                ...SWAP,
                vehicles: [
                    { name: HARPY, streams: HARPY_STREAMS, typed: true, problem: null },
                    { name: full(SWAP, 'kestrel'), streams: KESTREL_STREAMS, typed: true, problem: null },
                ],
                engines: engines(SWAP),
            },
            { ...LAB, vehicles: [], engines: engines(LAB) },
        ],
        unavailable: [],
        error: null,
    };
}

// A daemon known but unlistable, and two daemons whose hostnames share a first label.
export const HEISENBERG = { ip: '100.64.0.9', hostname: 'heisenberg', reason: 'refused' };
export const MOCK2 = { ip: '100.64.0.12', hostname: 'mock-100.64.0.12', local: false };
export const MOCK3 = { ip: '100.64.0.13', hostname: 'mock-100.64.0.13', local: false };

export function flowB() {
    return {
        schemaVersion: 3,
        name: 'early-discard',
        vehicle: HARPY,
        nodes: [
            { id: 'p1', kind: 'producer', stream: 'telemetry', tokens: 1, position: { x: 0, y: 0 } },
            { id: 'p2', kind: 'producer', stream: 'frames', tokens: 1, position: { x: 0, y: 150 } },
            { id: 'e1', kind: 'engine', engine: `${SWAP.ip}.yolo`, position: { x: 250, y: 150 } },
            { id: 'e2', kind: 'engine', engine: `${GCS.ip}.telemetry`, position: { x: 500, y: 0 } },
        ],
        edges: [
            { id: 'x1', kind: 'output', source: 'p1', target: 'e2', timeout: '2s' },
            { id: 'x2', kind: 'output', source: 'p2', target: 'e1', timeout: '1s' },
            { id: 'x3', kind: 'output', source: 'e1', target: 'e2', timeout: '2s' },
        ],
    };
}
