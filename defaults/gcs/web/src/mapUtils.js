import { displayName } from './vehicleNames.js';
import tokml from 'tokml';
import { kml as kmlToGeoJson } from '@tmcw/togeojson';
import ColorHash from 'color-hash';

const colorHash = new ColorHash();

// defaultFeatureName names a newly drawn feature "<Type>_<n>" (e.g.
// Polygon_1): the compiler turns each feature name into a Go constant
// (params.Polygon_<name>), so names must not contain spaces. n starts after
// the features of that type already drawn and skips any name in use.
export function defaultFeatureName(type, features) {
    const used = new Set(features.map(f => f.properties?.name).filter(Boolean));
    let n = features.filter(f => f.geometry?.type === type).length + 1;
    while (used.has(`${type}_${n}`)) n++;
    return `${type}_${n}`;
}

export function vehicleColor(name) {
    return colorHash.hex(name);
}

export function vehicleSpeed(velocity) {
    if (!velocity) return 0;
    return Math.sqrt(velocity.x_vel ** 2 + velocity.y_vel ** 2 + velocity.z_vel ** 2);
}

// How old the latest telemetry may be (seconds) for it to count as flowing.
export const TELEMETRY_STALE_SECONDS = 5;

// Whether the vehicle's telemetry is reaching the GCS. A vehicle without it
// may still be controllable, e.g. after a bad dataflow was pushed.
export function hasTelemetry(vehicle) {
    return vehicle.last_updated <= TELEMETRY_STALE_SECONDS;
}

// Whether the vehicle can be selected (and so held or controlled): its
// service is running on a daemon that answers, even if none of its data is
// reaching the GCS -- holding it is how its data gets flowing again. A
// vehicle whose daemon is unavailable (reachable: false) can't be.
export function isVehicleSelectable(vehicle) {
    return vehicle.reachable !== false;
}

// Whether control messages can't reach the vehicle: its daemon is
// unavailable. This alone makes a vehicle "offline"; missing telemetry or
// video doesn't (see hasTelemetry).
export function isVehicleDisconnected(vehicle) {
    return !isVehicleSelectable(vehicle);
}

// A single source of truth for "what state is this vehicle in," reused by
// every place that renders a vehicle's status. Selection overrides offline:
// a vehicle with no data can still be selected, to hold it, and must show
// that it is.
export function vehicleStatus(disconnected, selected) {
    if (selected) return 'selected';
    if (disconnected) return 'offline';
    return 'online';
}

// Sidebar card border/tint, collapsed rail dot, and map marker/popup all
// key off this triad so the three states read the same way everywhere:
// red = offline, blue = online-but-not-selected, green = selected. Raw
// --red-500/--blue-500/--green-500 (unlike the old muted --gray-* scheme)
// are saturated enough to stay legible on both the light/dark app themes
// and the always-dark map basemap, so no theme- or surface-specific
// tuning is needed here -- confirmed identical hex across lara-dark-amber
// and lara-light-blue theme files.
const SURFACE_STATUS_COLORS = {
    offline: 'var(--red-500)',
    online: 'var(--blue-500)',
    selected: 'var(--green-500)',
};

export function vehicleStatusColor(status) {
    return SURFACE_STATUS_COLORS[status];
}

// Collapsed rail dot fill. "offline" stays a dim grey fill (not red) so it
// still reads as "inactive" at a glance -- vehicleStatusColor's red is used
// only for its ring/border -- while "online"/"selected" fill solid with
// their status color.
export function vehicleStatusRailFill(status) {
    return status === 'offline' ? 'var(--gray-500)' : vehicleStatusColor(status);
}

// Map marker fill. "offline" matches the sidebar card's --red-500 (rather
// than a muted grey, tried first) -- grey markers all but disappeared
// against the satellite tileset's similarly muted browns/tans, whereas the
// saturated red stays legible on both it and the streets basemap. "online"
// likewise matches the sidebar card's --blue-500, so a vehicle's map
// marker and its status card always read as the same color.
const MAP_STATUS_COLORS = {
    offline: 'var(--red-500)',
    online: 'var(--blue-500)',
    selected: 'var(--green-500)',
};

export function vehicleStatusMapColor(status) {
    return MAP_STATUS_COLORS[status];
}

// Map popup label text. "offline" mirrors vehicleStatusMapColor's red for
// the same legibility-against-satellite-imagery reason. "online" stays a
// neutral light grey rather than matching its marker's blue, so the label
// doesn't compete with the marker's color; "selected" is white for
// contrast against its green marker.
const TEXT_STATUS_COLORS = {
    offline: 'var(--red-500)',
    online: 'var(--gray-200)',
    selected: '#ffffff',
};

export function vehicleStatusTextColor(status) {
    return TEXT_STATUS_COLORS[status];
}

const CARDINAL_DIRECTIONS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

// 8-point compass label for a bearing in degrees (0-360, wrapping either
// direction). Each direction spans 45 degrees centered on its own value,
// e.g. 324 deg rounds to 315 (NW), 180 deg lands exactly on S.
export function bearingToCardinal(bearing) {
    const normalized = ((bearing % 360) + 360) % 360;
    return CARDINAL_DIRECTIONS[Math.round(normalized / 45) % 8];
}

const STATUS_SORT_RANK = { selected: 0, online: 1, offline: 2 };

// Selected vehicles first, then online, then offline (sinks to the
// bottom); alphabetical by name within each group. squadList may be
// null/undefined (no manual selection yet).
export function sortVehiclesForDisplay(vehicles, squadList) {
    const squad = squadList ?? [];
    return [...vehicles].sort((a, b) => {
        const statusA = vehicleStatus(isVehicleDisconnected(a), squad.includes(a.name));
        const statusB = vehicleStatus(isVehicleDisconnected(b), squad.includes(b.name));
        const rankDiff = STATUS_SORT_RANK[statusA] - STATUS_SORT_RANK[statusB];
        if (rankDiff !== 0) return rankDiff;
        return a.name.localeCompare(b.name);
    });
}

export function featuresToGeoJson(featuresJson) {
    return JSON.stringify(JSON.parse(featuresJson), null, 2);
}

export function featuresToKml(featuresJson) {
    return tokml(JSON.parse(featuresJson));
}

export function parseImportFile(filename, text) {
    const ext = filename.split('.').pop().toLowerCase();
    if (ext === 'kml') {
        const dom = new DOMParser().parseFromString(text, 'text/xml');
        if (dom.querySelector('parsererror')) {
            throw new Error('Failed to parse KML: invalid XML');
        }
        const fc = kmlToGeoJson(dom);
        if (!fc || fc.type !== 'FeatureCollection') {
            throw new Error('KML did not produce a valid FeatureCollection');
        }
        return fc;
    }
    if (ext === 'geojson' || ext === 'json') {
        const fc = JSON.parse(text);
        if (!fc || fc.type !== 'FeatureCollection' || !Array.isArray(fc.features)) {
            throw new Error('Not a valid GeoJSON FeatureCollection');
        }
        return fc;
    }
    throw new Error('Unsupported file type. Use .kml, .geojson, or .json');
}

export function bboxFromFeature(feature) {
    const { type, coordinates } = feature.geometry;
    if (type === 'Point') {
        const [lng, lat] = coordinates;
        return [lng - 0.001, lat - 0.001, lng + 0.001, lat + 0.001];
    }
    const coords = type === 'Polygon' ? coordinates[0] : coordinates;
    let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
    for (const [lng, lat] of coords) {
        if (lng < minLng) minLng = lng;
        if (lat < minLat) minLat = lat;
        if (lng > maxLng) maxLng = lng;
        if (lat > maxLat) maxLat = lat;
    }
    return [minLng, minLat, maxLng, maxLat];
}

// How many altitudes vehicleTracksGeoJSON samples along each track, evenly
// spaced, for the map's line-z-offset.
export const TRACK_ELEVATION_SAMPLES = 64;

const EARTH_RADIUS_M = 6371000;

// The ground distance in meters between [long, lat, ...] points a and b, by
// the equirectangular approximation (fine at a track's scale).
function groundDistance(a, b) {
    const rad = Math.PI / 180;
    const x = (b[0] - a[0]) * rad * Math.cos(((a[1] + b[1]) / 2) * rad);
    const y = (b[1] - a[1]) * rad;
    return Math.hypot(x, y) * EARTH_RADIUS_M;
}

// The point `at` meters along the ground from the start of the path
// `coords` ([long, lat, alt] points, with `cum` their cumulative ground
// distances), interpolating long, lat and alt within its leg.
function pointAlong(coords, cum, at) {
    let i = 1;
    while (i < coords.length - 1 && cum[i] < at) i++;
    const leg = cum[i] - cum[i - 1];
    const f = leg > 0 ? Math.min(1, Math.max(0, (at - cum[i - 1]) / leg)) : 1;
    const a = coords[i - 1], b = coords[i];
    return [0, 1, 2].map(k => (a[k] ?? 0) + ((b[k] ?? 0) - (a[k] ?? 0)) * f);
}

// The cumulative ground distance to each point of coords.
function cumulative(coords) {
    const cum = [0];
    for (let i = 1; i < coords.length; i++) cum.push(cum[i - 1] + groundDistance(coords[i - 1], coords[i]));
    return cum;
}

// The vehicle's track ([long, lat, alt] points, oldest first) ending at its
// current position, or null if it has none.
function trackPath(v) {
    if (!v.track?.length) return null;
    const coords = v.track.slice();
    const here = [v.current.long, v.current.lat, v.current.alt];
    const last = coords[coords.length - 1];
    if (last[0] !== here[0] || last[1] !== here[1] || last[2] !== here[2]) coords.push(here);
    return coords.length < 2 ? null : coords;
}

function trackStatus(v, squadList) {
    return vehicleStatus(isVehicleDisconnected(v), !!squadList?.includes(v.name));
}

// Where each vehicle has been, as a GeoJSON FeatureCollection of
// LineStrings for a map line layer: the GCS's track of the vehicle
// ([long, lat, alt], oldest first), ending at its current position so the
// line meets its marker. Each feature's status (see vehicleStatus) colors it
// like the marker, and its elevation is the altitude at
// TRACK_ELEVATION_SAMPLES points evenly spaced along it, which the map
// indexes by line-progress to draw the line at the altitude flown. Vehicles
// without a track are left out.
export function vehicleTracksGeoJSON(vehicles, squadList) {
    const features = [];
    for (const v of vehicles) {
        const coordinates = trackPath(v);
        if (!coordinates) continue;
        const cum = cumulative(coordinates);
        const total = cum[cum.length - 1];
        const elevation = [];
        for (let k = 0; k < TRACK_ELEVATION_SAMPLES; k++) {
            elevation.push(pointAlong(coordinates, cum, (total * k) / (TRACK_ELEVATION_SAMPLES - 1))[2]);
        }
        features.push({
            type: 'Feature',
            geometry: { type: 'LineString', coordinates },
            properties: { name: v.name, status: trackStatus(v, squadList), elevation },
        });
    }
    return { type: 'FeatureCollection', features };
}

// Labels for the tracks of vehicleTracksGeoJSON: a Point every `spacing`
// meters along each track, back from the vehicle (whose marker labels it
// where it is), with the vehicle's display name, its status, and the
// altitude there.
export function vehicleTrackLabelsGeoJSON(vehicles, squadList, spacing) {
    const features = [];
    for (const v of vehicles) {
        const coordinates = trackPath(v);
        if (!coordinates) continue;
        const cum = cumulative(coordinates);
        const total = cum[cum.length - 1];
        const status = trackStatus(v, squadList);
        for (let back = spacing; back <= total; back += spacing) {
            const [long, lat, alt] = pointAlong(coordinates, cum, total - back);
            features.push({
                type: 'Feature',
                geometry: { type: 'Point', coordinates: [long, lat] },
                properties: { label: displayName(v.name), status, alt },
            });
        }
    }
    return { type: 'FeatureCollection', features };
}
