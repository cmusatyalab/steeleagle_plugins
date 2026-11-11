import { describe, it, expect } from 'vitest';
import { groundDistance } from './mapUtils.js';
import {
    CAMERA_MAX_RANGE_M, cameraRay, cameraFootprint, vehicleFootprintGeoJSON, setpointPath, cameraLabel, setpointLabel,
    vehicleCameraGeoJSON, vehicleSetpointGeoJSON,
} from './mapOverlays.js';

// A vehicle at [0, 0], 40 m up, flying 5 m/s, with fresh telemetry.
const vehicle = (extra = {}) => ({
    name: 'd1', last_updated: 0, reachable: true,
    current: { lat: 0, long: 0, alt: 40 },
    velocity: { x_vel: 3, y_vel: 4, z_vel: 0, angular_vel: 0 },
    ...extra,
});
const camera = (pitch, yaw = 90) => ({ camera: { yaw, pitch } });
const setpoint = (lat, alt = 40) => ({ setpoint: { lat, long: 0, alt } });

describe('cameraRay', () => {
    it('meets the ground where the camera centre points', () => {
        const ray = cameraRay(vehicle(camera(-30)));
        expect(groundDistance([0, 0], ray.end)).toBeCloseTo(69.28, 1);
        expect(ray.end[1]).toBeCloseTo(0, 9);
        expect(ray.end[0]).toBeGreaterThan(0);
        expect(ray.range).toBeCloseTo(69.28, 1);
        expect(ray.endAlt).toBe(0);
        expect(ray.clamped).toBe(false);
    });

    it('is cut at the max range when the ground is farther', () => {
        const ray = cameraRay(vehicle(camera(-5)));
        expect(ray.clamped).toBe(true);
        expect(groundDistance([0, 0], ray.end)).toBeCloseTo(CAMERA_MAX_RANGE_M, 1);
        expect(ray.endAlt).toBeCloseTo(26.88, 2);
    });

    it('is cut at the max range when pointing up (pitch up)', () => {
        const ray = cameraRay(vehicle(camera(10)));
        expect(ray.clamped).toBe(true);
        expect(ray.endAlt).toBeCloseTo(66.45, 2);
        expect([...ray.end, ray.endAlt, ray.range].every(Number.isFinite)).toBe(true);
    });

    it('is null without a camera, on the ground, or without fresh data', () => {
        expect(cameraRay(vehicle())).toBeNull();
        expect(cameraRay(vehicle({ ...camera(-30), current: { lat: 0, long: 0, alt: 0 } }))).toBeNull();
        expect(cameraRay(vehicle({ ...camera(-30), last_updated: 100 }))).toBeNull();
        expect(cameraRay(vehicle({ ...camera(-30), reachable: false }))).toBeNull();
    });
});

describe('setpointPath', () => {
    it('gives the distance and ETA to the setpoint', () => {
        const path = setpointPath(vehicle(setpoint(0.001)));
        expect(path.distance).toBeCloseTo(111.2, 1);
        expect(path.eta).toBeCloseTo(22.2, 1);
        expect(setpointLabel(path, 40)).toBe('111 m · 40 m · ETA 0:22');
    });

    it('has no ETA when hovering', () => {
        const path = setpointPath(vehicle({ ...setpoint(0.001), velocity: { x_vel: 0, y_vel: 0, z_vel: 0 } }));
        expect(path.eta).toBeNull();
        expect(setpointLabel(path, 40)).toBe('111 m · 40 m');
    });

    it('is null once arrived', () => {
        expect(setpointPath(vehicle(setpoint(0.00002, 41)))).toBeNull();
    });

    it('counts as arrived despite GPS and barometer altitude drift', () => {
        // 2.2 m away along the ground, 8 m off in altitude
        expect(setpointPath(vehicle(setpoint(0.00002, 48)))).toBeNull();
    });

    it('is null on the ground', () => {
        expect(setpointPath(vehicle({ ...setpoint(0.001), current: { lat: 0, long: 0, alt: 0 } }))).toBeNull();
    });

    it('is null without a setpoint or fresh data', () => {
        expect(setpointPath(vehicle())).toBeNull();
        expect(setpointPath(vehicle({ ...setpoint(0.001), last_updated: 100 }))).toBeNull();
    });
});

describe('cameraLabel', () => {
    it('gives range and pitch', () => {
        expect(cameraLabel({ range: 69.28 }, -30)).toBe('69 m · −30°');
    });
});

describe('vehicleCameraGeoJSON', () => {
    it('draws a line from each eligible vehicle to its ray end', () => {
        const fc = vehicleCameraGeoJSON([
            vehicle(camera(-30)),
            vehicle({ ...camera(-5), name: 'd2' }),
            vehicle({ name: 'd3' }),
        ], ['d2']);
        expect(fc.type).toBe('FeatureCollection');
        expect(fc.features).toHaveLength(2);
        const [a, b] = fc.features;
        expect(a.geometry.type).toBe('LineString');
        expect(a.geometry.coordinates[0]).toEqual([0, 0]);
        expect(a.properties).toEqual({ name: 'd1', status: 'online', elevation: [40, 0] });
        expect(b.properties.status).toBe('selected');
        expect(b.properties.elevation[1]).toBeCloseTo(26.88, 2);
    });
});

describe('vehicleSetpointGeoJSON', () => {
    it('draws only the selected vehicles\' setpoints', () => {
        const fc = vehicleSetpointGeoJSON([vehicle(setpoint(0.001))], []);
        expect(fc.features).toEqual([]);
    });

    it('draws a line from each eligible vehicle to its setpoint', () => {
        const fc = vehicleSetpointGeoJSON([
            vehicle(setpoint(0.001, 55)),
            vehicle({ name: 'd2' }),
        ], ['d1']);
        expect(fc.features).toHaveLength(1);
        expect(fc.features[0].geometry.coordinates).toEqual([[0, 0], [0, 0.001]]);
        expect(fc.features[0].properties).toEqual({ name: 'd1', status: 'selected', elevation: [40, 55] });
    });
});

// The north and east meters of [long, lat] p from [0, 0].
const northEast = (p) => [
    groundDistance([0, 0], [0, p[1]]) * Math.sign(p[1]),
    groundDistance([0, 0], [p[0], 0]) * Math.sign(p[0]),
];
const fov = (pitch, yaw = 0) => ({ camera: { yaw, pitch, hfov: 90, vfov: 60 } });

describe('cameraFootprint', () => {
    it('is the rectangle under a camera looking straight down', () => {
        const fp = cameraFootprint(vehicle(fov(-90)));
        expect(fp.clamped).toBe(false);
        // far-left, far-right, near-right, near-left: 40 tan 30 ahead/behind, 40 tan 45 aside
        const expected = [[23.09, -40], [23.09, 40], [-23.09, 40], [-23.09, -40]];
        fp.corners.forEach((c, i) => {
            const [n, e] = northEast(c);
            expect(n).toBeCloseTo(expected[i][0], 1);
            expect(e).toBeCloseTo(expected[i][1], 1);
        });
    });

    it('turns with the camera yaw', () => {
        const [n, e] = northEast(cameraFootprint(vehicle(fov(-90, 90))).corners[0]);
        // far-left of a camera facing east is north-east
        expect(n).toBeCloseTo(40, 1);
        expect(e).toBeCloseTo(23.09, 1);
    });

    it('is cut at the max range where the top of the frame misses the ground', () => {
        const fp = cameraFootprint(vehicle(fov(-30)));
        expect(fp.clamped).toBe(true);
        expect(groundDistance([0, 0], fp.corners[0])).toBeCloseTo(CAMERA_MAX_RANGE_M, 1);
        expect(groundDistance([0, 0], fp.corners[1])).toBeCloseTo(CAMERA_MAX_RANGE_M, 1);
        const [n, e] = northEast(fp.corners[2]);
        expect(n).toBeCloseTo(23.09, 1);
        expect(e).toBeCloseTo(40, 1);
        expect(fp.corners.flat().every(Number.isFinite)).toBe(true);
    });

    it('is null without a field of view or a camera ray', () => {
        expect(cameraFootprint(vehicle(camera(-30)))).toBeNull();
        expect(cameraFootprint(vehicle())).toBeNull();
        expect(cameraFootprint(vehicle({ ...fov(-30), last_updated: 100 }))).toBeNull();
    });
});

describe('vehicleFootprintGeoJSON', () => {
    it('gives each footprint as a closed polygon', () => {
        const fc = vehicleFootprintGeoJSON([vehicle(fov(-30)), vehicle({ name: 'd2' })]);
        expect(fc.features).toHaveLength(1);
        const ring = fc.features[0].geometry.coordinates[0];
        expect(fc.features[0].geometry.type).toBe('Polygon');
        expect(ring).toHaveLength(5);
        expect(ring[4]).toEqual(ring[0]);
        expect(fc.features[0].properties).toEqual({ name: 'd1', clamped: true });
    });
});
