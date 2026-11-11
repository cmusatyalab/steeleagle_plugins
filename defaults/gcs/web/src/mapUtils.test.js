// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { defaultFeatureName, featuresToGeoJson, featuresToKml, parseImportFile, bboxFromFeature, vehicleColor, vehicleSpeed, hasTelemetry, isVehicleDisconnected, isVehicleSelectable, vehicleStatus, vehicleStatusColor, vehicleStatusRailFill, vehicleStatusMapColor, vehicleStatusTextColor, sortVehiclesForDisplay, vehicleTracksGeoJSON, vehicleTrackLabelsGeoJSON, TRACK_ELEVATION_SAMPLES } from './mapUtils.js';

const SAMPLE_FC = {
    type: 'FeatureCollection',
    features: [{
        type: 'Feature',
        geometry: {
            type: 'Polygon',
            coordinates: [[
                [-79.94, 40.44], [-79.93, 40.44],
                [-79.93, 40.45], [-79.94, 40.44],
            ]],
        },
        properties: { name: 'TestArea' },
    }],
};
const SAMPLE_FC_JSON = JSON.stringify(SAMPLE_FC);

const SAMPLE_KML = `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>TestArea</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>-79.94,40.44,0 -79.93,40.44,0 -79.93,40.45,0 -79.94,40.44,0</coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>`;

describe('featuresToGeoJson', () => {
    it('round-trips a FeatureCollection', () => {
        const result = featuresToGeoJson(SAMPLE_FC_JSON);
        expect(JSON.parse(result)).toEqual(SAMPLE_FC);
    });

    it('pretty-prints the output', () => {
        const result = featuresToGeoJson(SAMPLE_FC_JSON);
        expect(result).toContain('\n');
    });
});

describe('featuresToKml', () => {
    it('produces a KML string with a Placemark', () => {
        const result = featuresToKml(SAMPLE_FC_JSON);
        expect(result).toContain('<kml');
        expect(result).toContain('<Placemark');
    });
});

describe('parseImportFile', () => {
    it('parses a valid .geojson file', () => {
        const fc = parseImportFile('areas.geojson', SAMPLE_FC_JSON);
        expect(fc.type).toBe('FeatureCollection');
        expect(fc.features).toHaveLength(1);
    });

    it('parses a .json file as GeoJSON', () => {
        const fc = parseImportFile('areas.json', SAMPLE_FC_JSON);
        expect(fc.type).toBe('FeatureCollection');
    });

    it('parses a valid .kml file and returns a FeatureCollection', () => {
        const fc = parseImportFile('areas.kml', SAMPLE_KML);
        expect(fc.type).toBe('FeatureCollection');
        expect(fc.features.length).toBeGreaterThan(0);
    });

    it('throws on unsupported extension', () => {
        expect(() => parseImportFile('data.csv', '{}')).toThrow('Unsupported file type');
    });

    it('throws on malformed GeoJSON', () => {
        expect(() => parseImportFile('bad.geojson', 'not json')).toThrow();
    });

    it('throws when GeoJSON root is not a FeatureCollection', () => {
        const notFc = JSON.stringify({ type: 'Feature', geometry: null, properties: {} });
        expect(() => parseImportFile('bad.geojson', notFc)).toThrow('Not a valid GeoJSON FeatureCollection');
    });

    it('throws on malformed KML (invalid XML)', () => {
        expect(() => parseImportFile('bad.kml', '<kml><broken')).toThrow('Failed to parse KML');
    });
});

describe('bboxFromFeature', () => {
    it('returns tight bbox for a polygon', () => {
        const feature = {
            type: 'Feature',
            geometry: {
                type: 'Polygon',
                coordinates: [[[-80, 40], [-79, 40], [-79, 41], [-80, 41], [-80, 40]]],
            },
            properties: {},
        };
        expect(bboxFromFeature(feature)).toEqual([-80, 40, -79, 41]);
    });

    it('returns tight bbox for a linestring', () => {
        const feature = {
            type: 'Feature',
            geometry: {
                type: 'LineString',
                coordinates: [[-80, 40], [-79, 41]],
            },
            properties: {},
        };
        expect(bboxFromFeature(feature)).toEqual([-80, 40, -79, 41]);
    });

    it('returns buffered bbox for a point', () => {
        const feature = {
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [-80, 40] },
            properties: {},
        };
        const bbox = bboxFromFeature(feature);
        expect(bbox[0]).toBeCloseTo(-80.001);
        expect(bbox[1]).toBeCloseTo(39.999);
        expect(bbox[2]).toBeCloseTo(-79.999);
        expect(bbox[3]).toBeCloseTo(40.001);
    });
});

describe('vehicleColor', () => {
    it('returns a hex color string', () => {
        expect(vehicleColor('vehicle-1')).toMatch(/^#[0-9a-f]{6}$/i);
    });

    it('is deterministic for the same name', () => {
        expect(vehicleColor('vehicle-1')).toBe(vehicleColor('vehicle-1'));
    });

    it('differs for different names', () => {
        expect(vehicleColor('vehicle-1')).not.toBe(vehicleColor('vehicle-2'));
    });
});

describe('vehicleSpeed', () => {
    it('returns 0 for a stationary vehicle', () => {
        expect(vehicleSpeed({ x_vel: 0, y_vel: 0, z_vel: 0 })).toBe(0);
    });

    it('returns the magnitude for a single-axis velocity', () => {
        expect(vehicleSpeed({ x_vel: 3, y_vel: 0, z_vel: 0 })).toBe(3);
    });

    it('computes the Euclidean norm across all three axes', () => {
        // 3-4-12 generalizes the 3-4-5 triple to three dimensions: sqrt(9+16+144) = 13
        expect(vehicleSpeed({ x_vel: 3, y_vel: 4, z_vel: 12 })).toBe(13);
    });

    it('treats negative components the same as positive (speed has no direction)', () => {
        expect(vehicleSpeed({ x_vel: -3, y_vel: -4, z_vel: 0 })).toBe(5);
    });

    it('returns 0 for a null or undefined velocity', () => {
        expect(vehicleSpeed(null)).toBe(0);
        expect(vehicleSpeed(undefined)).toBe(0);
    });
});

describe('hasTelemetry', () => {
    it('is true when telemetry was just seen', () => {
        expect(hasTelemetry({ last_updated: 0 })).toBe(true);
    });

    it('is true right at the boundary', () => {
        expect(hasTelemetry({ last_updated: 5 })).toBe(true);
    });

    it('is false once telemetry is stale beyond the boundary', () => {
        expect(hasTelemetry({ last_updated: 5.01 })).toBe(false);
    });
});

describe('isVehicleDisconnected', () => {
    it('is false for a controllable vehicle with no telemetry', () => {
        expect(isVehicleDisconnected({ reachable: true, last_updated: 1e6 })).toBe(false);
    });

    it('is true for a vehicle whose daemon is unavailable', () => {
        expect(isVehicleDisconnected({ reachable: false, last_updated: 1e6 })).toBe(true);
    });
});

describe('isVehicleSelectable', () => {
    it('is true for a vehicle its daemon lists, even with no data reaching the GCS', () => {
        expect(isVehicleSelectable({ reachable: true, last_updated: 1e6 })).toBe(true);
    });

    it('is false for a vehicle whose daemon is unavailable', () => {
        expect(isVehicleSelectable({ reachable: false, last_updated: 1e6 })).toBe(false);
    });
});

describe('vehicleStatus', () => {
    it('is "offline" when disconnected and not selected', () => {
        expect(vehicleStatus(true, false)).toBe('offline');
    });

    it('is "selected" when selected, even if disconnected, so it can be held', () => {
        expect(vehicleStatus(true, true)).toBe('selected');
    });

    it('is "selected" when connected and selected', () => {
        expect(vehicleStatus(false, true)).toBe('selected');
    });

    it('is "online" when connected and not selected', () => {
        expect(vehicleStatus(false, false)).toBe('online');
    });
});

describe('vehicleStatusColor', () => {
    it('returns a distinct color per status', () => {
        const colors = ['offline', 'online', 'selected'].map(vehicleStatusColor);
        expect(new Set(colors).size).toBe(3);
    });

    it('uses red for offline, blue for online, and (unchanged) green for selected', () => {
        expect(vehicleStatusColor('offline')).toBe('var(--red-500)');
        expect(vehicleStatusColor('online')).toBe('var(--blue-500)');
        expect(vehicleStatusColor('selected')).toBe('var(--green-500)');
    });
});

describe('vehicleStatusRailFill', () => {
    it('keeps a dim grey fill for offline so it still reads as inactive, not just red', () => {
        expect(vehicleStatusRailFill('offline')).toBe('var(--gray-500)');
    });

    it('matches vehicleStatusColor for online and selected', () => {
        expect(vehicleStatusRailFill('online')).toBe(vehicleStatusColor('online'));
        expect(vehicleStatusRailFill('selected')).toBe(vehicleStatusColor('selected'));
    });
});

describe('vehicleStatusMapColor', () => {
    it('returns a distinct color per status', () => {
        const colors = ['offline', 'online', 'selected'].map(vehicleStatusMapColor);
        expect(new Set(colors).size).toBe(3);
    });

    it('matches vehicleStatusColor\'s blue for "online", so a vehicle\'s marker and card read as the same color', () => {
        expect(vehicleStatusMapColor('online')).toBe(vehicleStatusColor('online'));
    });

    it('uses a muted grey (not red) for "offline" -- red markers scattered across the map read as too busy', () => {
        expect(vehicleStatusMapColor('offline')).toBe('var(--gray-500)');
    });
});

describe('vehicleStatusTextColor', () => {
    it('returns a distinct color per status', () => {
        const colors = ['offline', 'online', 'selected'].map(vehicleStatusTextColor);
        expect(new Set(colors).size).toBe(3);
    });

    it('uses white for the selected status, for contrast against its green marker', () => {
        expect(vehicleStatusTextColor('selected')).toBe('#ffffff');
    });

    it('stays a neutral grey scale for offline/online, independent of the blue marker chevron', () => {
        expect(vehicleStatusTextColor('offline')).toBe('var(--gray-500)');
        expect(vehicleStatusTextColor('online')).toBe('var(--gray-200)');
        expect(vehicleStatusTextColor('online')).not.toBe(vehicleStatusMapColor('online'));
    });
});

describe('sortVehiclesForDisplay', () => {
    const v = (name, reachable = true) => ({ name, reachable, last_updated: 0 });

    it('puts selected vehicles before online, and online before offline', () => {
        const vehicles = [v('offline-1', false), v('online-1'), v('selected-1')];
        const result = sortVehiclesForDisplay(vehicles, ['selected-1']);
        expect(result.map((x) => x.name)).toEqual(['selected-1', 'online-1', 'offline-1']);
    });

    it('sorts alphabetically by name within the same status group', () => {
        const vehicles = [v('bravo'), v('alpha'), v('charlie')];
        const result = sortVehiclesForDisplay(vehicles, []);
        expect(result.map((x) => x.name)).toEqual(['alpha', 'bravo', 'charlie']);
    });

    it('treats a null squadList as no selection', () => {
        const vehicles = [v('a'), v('b')];
        const result = sortVehiclesForDisplay(vehicles, null);
        expect(result.map((x) => x.name)).toEqual(['a', 'b']);
    });

    it('does not mutate the input array', () => {
        const vehicles = [v('b'), v('a')];
        sortVehiclesForDisplay(vehicles, []);
        expect(vehicles.map((x) => x.name)).toEqual(['b', 'a']);
    });
});

describe('defaultFeatureName', () => {
    const named = (type, name) => ({ type: 'Feature', geometry: { type }, properties: { name } });

    it('numbers features per geometry type with no spaces', () => {
        expect(defaultFeatureName('Polygon', [])).toBe('Polygon_1');
        expect(defaultFeatureName('LineString', [named('Polygon', 'Polygon_1')])).toBe('LineString_1');
        expect(defaultFeatureName('Point', [named('Point', 'Point_1'), named('Point', 'Home')])).toBe('Point_3');
    });

    it('skips names already in use, whatever their type', () => {
        const features = [named('Polygon', 'Polygon_1'), named('Point', 'Polygon_2')];
        expect(defaultFeatureName('Polygon', features)).toBe('Polygon_3');
    });

    it('ignores unnamed features when checking for collisions', () => {
        expect(defaultFeatureName('Polygon', [{ geometry: { type: 'Polygon' }, properties: {} }])).toBe('Polygon_2');
    });
});

describe('vehicleTracksGeoJSON', () => {
    const v = (name, track, extra = {}) => ({
        name, last_updated: 0, reachable: true,
        current: { lat: 40.3, long: -79.7, alt: 10 }, track, ...extra,
    });

    it('draws each track to the vehicle\'s current position', () => {
        const fc = vehicleTracksGeoJSON([v('d1', [[-79.9, 40.1, 0], [-79.8, 40.2, 5]])], []);
        expect(fc.type).toBe('FeatureCollection');
        expect(fc.features).toHaveLength(1);
        expect(fc.features[0].geometry).toEqual({
            type: 'LineString',
            coordinates: [[-79.9, 40.1, 0], [-79.8, 40.2, 5], [-79.7, 40.3, 10]],
        });
        expect(fc.features[0].properties.name).toBe('d1');
        expect(fc.features[0].properties.status).toBe('online');
    });

    it('carries the vehicle status for coloring', () => {
        const fc = vehicleTracksGeoJSON([
            v('d1', [[-79.9, 40.1, 0]]),
            v('d2', [[-79.9, 40.1, 0]], { last_updated: 1e6, reachable: false }),
        ], ['d1']);
        expect(fc.features.map(f => f.properties.status)).toEqual(['selected', 'offline']);
    });

    it('skips vehicles without a track', () => {
        const fc = vehicleTracksGeoJSON([v('d1', undefined), v('d2', [])], []);
        expect(fc.features).toEqual([]);
    });

    it('does not repeat the current position when it ends the track', () => {
        const fc = vehicleTracksGeoJSON([v('d1', [[-79.9, 40.1, 0], [-79.7, 40.3, 10]])], []);
        expect(fc.features[0].geometry.coordinates).toEqual([[-79.9, 40.1, 0], [-79.7, 40.3, 10]]);
    });

    it('samples the altitude evenly along the line, for line-z-offset', () => {
        // Along a meridian: the first leg is three times the second
        const track = [[-79.7, 40.0, 0], [-79.7, 40.3, 30]];
        const fc = vehicleTracksGeoJSON([v('d1', track, { current: { lat: 40.4, long: -79.7, alt: 40 } })], []);
        const elevation = fc.features[0].properties.elevation;
        expect(elevation).toHaveLength(TRACK_ELEVATION_SAMPLES);
        expect(elevation[0]).toBeCloseTo(0);
        expect(elevation[TRACK_ELEVATION_SAMPLES - 1]).toBeCloseTo(40);
        // Three quarters along is the end of the first leg; the nearest
        // sample is within a sample's spacing of it
        const i = Math.round((TRACK_ELEVATION_SAMPLES - 1) * 0.75);
        expect(elevation[i]).toBeCloseTo(30, 0);
    });
});

describe('vehicleTrackLabelsGeoJSON', () => {
    // 0.001 degrees of latitude is ~111 m
    const v = (name, track, extra = {}) => ({
        name, last_updated: 0, reachable: true,
        current: { lat: 40.002, long: -79.7, alt: 20 }, track, ...extra,
    });

    // Straight north from 40.0, a point every ~111 m
    const north = (from, n) => Array.from({ length: n }, (_, i) => [-79.7, from + i * 0.001, 0]);
    const latAt = (m) => 40.0 + m / 111195;

    it('labels the track every so many meters of the path flown', () => {
        const fc = vehicleTrackLabelsGeoJSON([v('100.64.0.1.alpha', north(40.0, 3))], [], 50);
        // ~222 m recorded: labels at 50, 100 and 150 m; 200 m is too near the vehicle
        expect(fc.features).toHaveLength(3);
        const f = fc.features[0];
        expect(f.geometry.type).toBe('Point');
        expect(f.geometry.coordinates[0]).toBeCloseTo(-79.7);
        expect(f.geometry.coordinates[1]).toBeCloseTo(latAt(50), 6);
        expect(f.properties).toMatchObject({ label: 'alpha', status: 'online' });
        expect(fc.features.map((g) => g.geometry.coordinates[1])).toEqual(
            [50, 100, 150].map((m) => expect.closeTo(latAt(m), 6)));
    });

    it('keeps each label where it was as the track moves on', () => {
        const before = vehicleTrackLabelsGeoJSON([v('d1', north(40.0, 3))], [], 50);
        // Flown ~111 m on: the oldest point dropped, ~111 m flown before the rest
        const after = vehicleTrackLabelsGeoJSON([v('d1', north(40.001, 3), {
            current: { lat: 40.003, long: -79.7, alt: 0 }, track_start: 111.195,
        })], [], 50);
        const lats = (fc) => fc.features.map((f) => f.geometry.coordinates[1]);
        // 150 m is in both, at the same spot; 200, 250 and 300 m are new
        expect(lats(after)).toEqual([150, 200, 250, 300].map((m) => expect.closeTo(latAt(m), 6)));
        expect(lats(after)[0]).toBeCloseTo(lats(before)[2], 8);
    });

    it('ignores the live leg to the vehicle\'s current position', () => {
        const fc = vehicleTrackLabelsGeoJSON([v('d1', north(40.0, 2), {
            current: { lat: 40.003, long: -79.7, alt: 0 },
        })], [], 50);
        // Only the ~111 m recorded: 50 m; 100 m is too near the newest point
        expect(fc.features).toHaveLength(1);
    });

    it('labels nothing on a track shorter than the spacing', () => {
        const fc = vehicleTrackLabelsGeoJSON([v('d1', [[-79.7, 40.0019, 0]])], [], 50);
        expect(fc.features).toEqual([]);
    });
});
