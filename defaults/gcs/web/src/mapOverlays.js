// Geometry for the map's per-vehicle overlays: the line from a vehicle to
// where its gimbal camera's centre meets the ground, and the line from a
// vehicle to the position it was sent to (its setpoint). The ground is taken
// as flat at the takeoff altitude, which current.alt is relative to.
import { groundDistance, hasTelemetry, isVehicleDisconnected, vehicleStatus } from './mapUtils.js';

// The farthest along the ground a camera line reaches: a camera near level
// or pointing up would meet the ground far away or never.
export const CAMERA_MAX_RANGE_M = 150;
// A setpoint this close to the vehicle along the ground, and within
// SETPOINT_ARRIVED_ALT_M of its altitude, is reached, and not drawn: drivers
// may keep reporting it after arrival. The altitude tolerance is loose since
// the setpoint's altitude comes from GPS and the vehicle's from a barometer.
export const SETPOINT_ARRIVED_M = 3;
export const SETPOINT_ARRIVED_ALT_M = 10;
// Below this horizontal speed (m/s), no ETA is given.
const ETA_MIN_SPEED = 0.5;

const EARTH_RADIUS_M = 6371000;
const RAD = Math.PI / 180;

// The point distM meters along the ground from [long, lat] at bearingDeg
// (clockwise from north), by the equirectangular approximation.
export function offsetPoint(long, lat, bearingDeg, distM) {
    return offsetNorthEast(long, lat, distM * Math.cos(bearingDeg * RAD), distM * Math.sin(bearingDeg * RAD));
}

// The point north and east meters from [long, lat].
function offsetNorthEast(long, lat, north, east) {
    return [
        long + (east / (EARTH_RADIUS_M * Math.cos(lat * RAD))) / RAD,
        lat + (north / EARTH_RADIUS_M) / RAD,
    ];
}

// Whether the vehicle's overlays can be drawn: its data is fresh and its
// daemon answers, so they show where it is now.
function eligible(v) {
    return hasTelemetry(v) && !isVehicleDisconnected(v);
}

// The vehicle's camera ray: where it ends ([long, lat]) and at what altitude,
// its range along the ground to the ground hit, and whether it was cut at
// maxRange short of the ground (then endAlt is the ray's altitude there).
// Null if the vehicle has no camera, is on the ground, or isn't eligible.
export function cameraRay(v, maxRange = CAMERA_MAX_RANGE_M) {
    if (!v.camera || !(v.current.alt > 0) || !eligible(v)) return null;
    const { yaw, pitch } = v.camera;
    const alt = v.current.alt;
    const range = pitch < 0 ? alt / Math.tan(-pitch * RAD) : Infinity;
    const clamped = range > maxRange;
    const d = Math.min(range, maxRange);
    const endAlt = clamped ? Math.max(0, alt + d * Math.tan(pitch * RAD)) : 0;
    return { end: offsetPoint(v.current.long, v.current.lat, yaw, d), endAlt, range: d, clamped };
}

// The patch of ground the vehicle's camera sees, from its yaw, pitch and
// field of view (taking roll as 0): its corners ([long, lat]) far-left,
// far-right, near-right, near-left, and whether any corner ray misses the
// ground within maxRange and was cut there. Null without a field of view, or
// where cameraRay is null.
export function cameraFootprint(v, maxRange = CAMERA_MAX_RANGE_M) {
    const { camera } = v;
    if (!(camera?.hfov > 0 && camera?.vfov > 0) || !cameraRay(v, maxRange)) return null;
    const alt = v.current.alt;
    const p = camera.pitch * RAD, yaw = camera.yaw * RAD;
    const th = Math.tan((camera.hfov / 2) * RAD), tv = Math.tan((camera.vfov / 2) * RAD);
    let clamped = false;
    // Each corner ray, as (top: +1 far, -1 near; side: -1 left, +1 right), in
    // forward/right/up meters from the camera, scaled to meet the ground.
    const corners = [[1, -1], [1, 1], [-1, 1], [-1, -1]].map(([top, side]) => {
        const fwd = Math.cos(p) - tv * top * Math.sin(p);
        const right = th * side;
        const up = Math.sin(p) + tv * top * Math.cos(p);
        const horizontal = Math.hypot(fwd, right);
        let k = up < 0 ? alt / -up : Infinity;
        if (k * horizontal > maxRange) {
            k = maxRange / horizontal;
            clamped = true;
        }
        const x = k * fwd, y = k * right;
        return offsetNorthEast(v.current.long, v.current.lat,
            x * Math.cos(yaw) - y * Math.sin(yaw), x * Math.sin(yaw) + y * Math.cos(yaw));
    });
    return { corners, clamped };
}

// Each eligible vehicle's camera footprint (see cameraFootprint), as a
// FeatureCollection of Polygons.
export function vehicleFootprintGeoJSON(vehicles) {
    const features = [];
    for (const v of vehicles) {
        const fp = cameraFootprint(v);
        if (!fp) continue;
        features.push({
            type: 'Feature',
            geometry: { type: 'Polygon', coordinates: [[...fp.corners, fp.corners[0]]] },
            properties: { name: v.name, clamped: fp.clamped },
        });
    }
    return { type: 'FeatureCollection', features };
}

// The path to the vehicle's setpoint: its distance along the ground and the
// ETA in seconds at the current horizontal speed (null if hovering). Null if
// there is no setpoint, the vehicle is on the ground or has reached it, or it
// isn't eligible.
export function setpointPath(v) {
    if (!v.setpoint || !(v.current.alt > 0) || !eligible(v)) return null;
    const distance = groundDistance([v.current.long, v.current.lat], [v.setpoint.long, v.setpoint.lat]);
    if (distance < SETPOINT_ARRIVED_M && Math.abs(v.setpoint.alt - v.current.alt) < SETPOINT_ARRIVED_ALT_M) return null;
    const speed = v.velocity ? Math.hypot(v.velocity.x_vel, v.velocity.y_vel) : 0;
    return { distance, eta: speed >= ETA_MIN_SPEED ? distance / speed : null };
}

// Degrees with a true minus sign, e.g. "−30°".
const degrees = (d) => `${Math.round(d)}°`.replace('-', '−');

// The camera crosshair's label: range and pitch, e.g. "69 m · −30°".
export function cameraLabel(ray, pitch) {
    return `${Math.round(ray.range)} m · ${degrees(pitch)}`;
}

// The setpoint's label: distance, altitude and ETA (if any), e.g.
// "111 m · 40 m · ETA 0:22".
export function setpointLabel(path, setpointAlt) {
    const parts = [`${Math.round(path.distance)} m`, `${Math.round(setpointAlt)} m`];
    if (path.eta !== null) {
        const s = Math.round(path.eta);
        parts.push(`ETA ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`);
    }
    return parts.join(' · ');
}

// A LineString from the vehicle to end, with its status for colouring and the
// altitude at each end, which the map indexes by line-progress to draw it in
// the air.
function lineFeature(v, squadList, end, endAlt) {
    return {
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: [[v.current.long, v.current.lat], end] },
        properties: {
            name: v.name,
            status: vehicleStatus(isVehicleDisconnected(v), !!squadList?.includes(v.name)),
            elevation: [v.current.alt, endAlt],
        },
    };
}

// Each eligible vehicle's camera line (see cameraRay), as a FeatureCollection.
export function vehicleCameraGeoJSON(vehicles, squadList) {
    const features = [];
    for (const v of vehicles) {
        const ray = cameraRay(v);
        if (ray) features.push(lineFeature(v, squadList, ray.end, ray.endAlt));
    }
    return { type: 'FeatureCollection', features };
}

// Each selected, eligible vehicle's line to its setpoint (see setpointPath),
// as a FeatureCollection.
export function vehicleSetpointGeoJSON(vehicles, squadList) {
    const features = [];
    for (const v of vehicles) {
        if (!squadList?.includes(v.name) || !setpointPath(v)) continue;
        features.push(lineFeature(v, squadList, [v.setpoint.long, v.setpoint.lat], v.setpoint.alt));
    }
    return { type: 'FeatureCollection', features };
}
