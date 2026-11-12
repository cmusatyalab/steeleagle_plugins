// Detection pins: objects engines found, placed on the ground and grouped by
// the GCS (internal/pins), as /api/pins serves them. Helpers for drawing them
// on the map and for the Detections settings.
import { getApiUrl } from './urls.js';
import { displayName } from './vehicleNames.js';

// A pin starts fading once this fraction of the expiry has passed since its
// last sighting, down to MIN_OPACITY at expiry.
export const PIN_FADE_START = 0.5;
const MIN_OPACITY = 0.35;

// Distinct colours for classes, readable on both the streets and satellite
// styles.
const PALETTE = [
    '#e57373', '#ffb74d', '#81c784', '#64b5f6', '#ba68c8',
    '#4dd0e1', '#f06292', '#aed581', '#ffd54f', '#9575cd',
];

// The colour of a class: always the same for the same class.
export function classColor(cls) {
    let h = 0;
    for (const c of cls) h = (Math.imul(h, 31) + c.codePointAt(0)) | 0;
    return PALETTE[Math.abs(h) % PALETTE.length];
}

// How opaque a pin last seen lastSeenS seconds ago is, given the expiry.
export function pinOpacity(lastSeenS, expiryS) {
    const start = PIN_FADE_START * expiryS;
    if (!(expiryS > 0) || lastSeenS <= start) return 1;
    const f = Math.min(1, (lastSeenS - start) / (expiryS - start));
    return 1 - f * (1 - MIN_OPACITY);
}

// How long ago s seconds was, e.g. "4 s ago", "2 min ago", "1 h ago".
export function relativeTime(s) {
    if (s < 60) return `${Math.floor(s)} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    return `${Math.floor(s / 3600)} h ago`;
}

function escapeHTML(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// The hover card of a pin, as HTML; every value is escaped, since classes
// and names come from engines and vehicles.
export function pinCardHTML(pin) {
    const seen = pin.sightings === 1 ? 'once' : `${pin.sightings} times`;
    const lines = [
        `<strong>${escapeHTML(pin.class)}</strong> via ${escapeHTML(displayName(pin.engine))}`,
        `Seen: ${escapeHTML(seen)} · By: ${escapeHTML(pin.vehicles.map(displayName).join(', '))}`,
        `First: ${escapeHTML(relativeTime(pin.first_seen_s))} · Last: ${escapeHTML(relativeTime(pin.last_seen_s))}`,
    ];
    return `<div style="color:black;line-height:1.5;text-align:left">${lines.join('<br>')}</div>`;
}

// How many pins there are of each class, sorted by class.
export function classCounts(pins) {
    const counts = new Map();
    for (const p of pins) counts.set(p.class, (counts.get(p.class) ?? 0) + 1);
    return [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([cls, count]) => ({ cls, count }));
}

// The pins whose class isn't hidden.
export function visiblePins(pins, hiddenClasses) {
    return pins.filter((p) => !hiddenClasses.has(p.class));
}

async function request(method, path, body) {
    const init = { method };
    if (body !== undefined) {
        init.headers = { 'Content-Type': 'application/json' };
        init.body = JSON.stringify(body);
    }
    const response = await fetch(getApiUrl(path), init);
    let result = null;
    try {
        result = await response.json();
    } catch {
        // checked below
    }
    if (!response.ok) throw new Error(result?.detail ?? `HTTP ${response.status}`);
    if (result === null) throw new Error('server returned a non-JSON response');
    return result;
}

// The pins and the shared settings: {settings, pins}.
export function fetchPins() {
    return request('GET', '/api/pins');
}

// Replaces the shared settings; resolves to them as the server keeps them.
export function putPinSettings(settings) {
    return request('PUT', '/api/pins/settings', settings);
}

// Removes every pin for everyone; resolves to {cleared}.
export function clearPins() {
    return request('POST', '/api/pins/clear', {});
}
