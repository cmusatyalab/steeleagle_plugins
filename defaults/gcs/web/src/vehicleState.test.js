import { describe, it, expect } from 'vitest';
import { vehicleBadges, alertEvents, badgesHTML } from './vehicleState.js';

const vehicle = (extra = {}) => ({ name: '100.64.0.1.alpha', last_updated: 0, auto: false, mode: '', motion: '', alerts: {}, ...extra });
const labels = (v) => vehicleBadges(v).map((b) => b.label);

describe('vehicleBadges', () => {
    it('always shows AUTO or MANUAL', () => {
        expect(labels(vehicle())).toEqual(['MANUAL']);
        expect(labels(vehicle({ auto: true }))).toEqual(['AUTO']);
        expect(labels(vehicle({ auto: true, last_updated: 1e6 }))).toEqual(['AUTO']);
    });
    it('shows TAKEOFF, LAND and RTH only while in transit', () => {
        for (const [mode, label] of [['TAKEOFF', 'TAKEOFF'], ['LAND', 'LAND'], ['RETURN_TO_HOME', 'RTH']]) {
            expect(labels(vehicle({ mode, motion: 'IN_TRANSIT' }))).toEqual(['MANUAL', label]);
            expect(labels(vehicle({ mode, motion: 'HOLDING' }))).toEqual(['MANUAL']);
            expect(labels(vehicle({ mode, motion: 'STOPPED' }))).toEqual(['MANUAL']);
        }
    });
    it('always shows EMERG', () => {
        expect(labels(vehicle({ mode: 'EMERGENCY', motion: 'STOPPED' }))).toEqual(['MANUAL', 'EMERG']);
    });
    it('shows no badge for loiter or guided', () => {
        expect(labels(vehicle({ mode: 'LOITER', motion: 'IN_TRANSIT' }))).toEqual(['MANUAL']);
        expect(labels(vehicle({ mode: 'GUIDED', motion: 'IN_TRANSIT' }))).toEqual(['MANUAL']);
    });
    it('hides the mode badge when telemetry is stale', () => {
        expect(labels(vehicle({ mode: 'EMERGENCY', last_updated: 1e6 }))).toEqual(['MANUAL']);
    });
    it('gives each badge a kind for its colour', () => {
        expect(vehicleBadges(vehicle({ auto: true, mode: 'EMERGENCY' })).map((b) => b.kind)).toEqual(['auto', 'emerg']);
    });
});

describe('alertEvents', () => {
    it('toasts every alert present when telemetry first arrives', () => {
        const ev = alertEvents(undefined, { battery: 'LOW', gps: 'NO_FIX' });
        expect(ev).toEqual([
            { key: 'battery', severity: 'warn', text: 'battery low' },
            { key: 'gps', severity: 'error', text: 'no GPS fix' },
        ]);
    });
    it('toasts an alert that appears or worsens, not one that persists or eases', () => {
        expect(alertEvents({ battery: 'LOW' }, { battery: 'LOW' })).toEqual([]);
        expect(alertEvents({ battery: 'LOW' }, { battery: 'CRITICAL' })).toEqual([{ key: 'battery', severity: 'error', text: 'battery critical' }]);
        expect(alertEvents({ battery: 'CRITICAL' }, { battery: 'LOW' })).toEqual([]);
        expect(alertEvents({ connection: 'WEAK_CONNECTION' }, { connection: 'DISCONNECTED' })).toEqual([{ key: 'connection', severity: 'error', text: 'connection lost' }]);
        expect(alertEvents({}, {})).toEqual([]);
    });
    it('toasts again after an alert clears and returns', () => {
        expect(alertEvents({}, { magnetometer: 'PERTURBATIONS' })).toEqual([{ key: 'magnetometer', severity: 'warn', text: 'magnetometer perturbation' }]);
    });
    it('names every level with its severity', () => {
        const all = { battery: 'CRITICAL', gps: 'WEAK_SIGNAL', magnetometer: 'PERTURBATIONS', connection: 'WEAK_CONNECTION', compass: 'NO_LOCK' };
        expect(alertEvents({}, all).map((e) => [e.text, e.severity])).toEqual([
            ['battery critical', 'error'], ['weak GPS signal', 'warn'], ['magnetometer perturbation', 'warn'],
            ['weak connection', 'warn'], ['no compass lock', 'error'],
        ]);
    });
    it('ignores unknown levels and missing alerts', () => {
        expect(alertEvents({}, { battery: 'SOMETHING_NEW' })).toEqual([]);
        expect(alertEvents({}, undefined)).toEqual([]);
    });
});

describe('badgesHTML', () => {
    it('renders each badge as a span with its kind', () => {
        expect(badgesHTML(vehicle({ auto: true, mode: 'RETURN_TO_HOME', motion: 'IN_TRANSIT' })))
            .toBe('<span class="vehicle-badge vehicle-badge-auto">AUTO</span><span class="vehicle-badge vehicle-badge-rth">RTH</span>');
    });
});
