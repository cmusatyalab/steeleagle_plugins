// What a vehicle is doing, for its badges on the map and status card, and
// the alerts it raises, for toasts. Read from /api/remote/vehicles: auto,
// mode, motion and alerts (see fleet.VehicleJSON).
import { hasTelemetry } from './mapUtils.js';

// The modes worth a badge, by the driver's mode name. TAKEOFF, LAND and RTH
// only while the vehicle is in transit: drivers such as the Anafi's keep
// reporting the last mode they were sent after it is done.
const MODE_BADGES = {
    TAKEOFF: { label: 'TAKEOFF', kind: 'takeoff', whileMoving: true },
    LAND: { label: 'LAND', kind: 'land', whileMoving: true },
    RETURN_TO_HOME: { label: 'RTH', kind: 'rth', whileMoving: true },
    EMERGENCY: { label: 'EMERG', kind: 'emerg', whileMoving: false },
};

// The vehicle's badges, as {label, kind}: AUTO while a mission this GCS
// started runs on it, else MANUAL; then its mode, if worth a badge and its
// telemetry is fresh.
export function vehicleBadges(v) {
    const badges = [v.auto ? { label: 'AUTO', kind: 'auto' } : { label: 'MANUAL', kind: 'manual' }];
    const mode = MODE_BADGES[v.mode];
    if (mode && hasTelemetry(v) && (!mode.whileMoving || v.motion === 'IN_TRANSIT')) {
        badges.push({ label: mode.label, kind: mode.kind });
    }
    return badges;
}

// Each alert's levels, mildest first, with their toast severity and text.
const ALERTS = [
    ['battery', [['LOW', 'warn', 'battery low'], ['CRITICAL', 'error', 'battery critical']]],
    ['gps', [['WEAK_SIGNAL', 'warn', 'weak GPS signal'], ['NO_FIX', 'error', 'no GPS fix']]],
    ['magnetometer', [['PERTURBATIONS', 'warn', 'magnetometer perturbation']]],
    ['connection', [['WEAK_CONNECTION', 'warn', 'weak connection'], ['DISCONNECTED', 'error', 'connection lost']]],
    ['compass', [['WEAK_LOCK', 'warn', 'weak compass lock'], ['NO_LOCK', 'error', 'no compass lock']]],
];

// The level's rank among the alert's levels (1 mildest), 0 if none or unknown.
const rank = (levels, level) => levels.findIndex(([name]) => name === level) + 1;

// The alerts to toast, as {key, severity, text}: those in alerts that are
// new or worse than in prev. prev is undefined for a vehicle whose
// telemetry just arrived, so every alert it has is toasted once.
export function alertEvents(prev, alerts) {
    const events = [];
    for (const [key, levels] of ALERTS) {
        const now = rank(levels, alerts?.[key]);
        if (now > rank(levels, prev?.[key])) {
            const [, severity, text] = levels[now - 1];
            events.push({ key, severity, text });
        }
    }
    return events;
}

// The vehicle's badges as HTML, for the map's labels; styled in App.css.
// Labels and kinds are fixed strings, so there's nothing to escape.
export function badgesHTML(v) {
    return vehicleBadges(v).map((b) => `<span class="vehicle-badge vehicle-badge-${b.kind}">${b.label}</span>`).join('');
}
