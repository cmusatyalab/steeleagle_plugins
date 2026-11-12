import { useRef, useEffect } from 'react'
import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css';
import { vehicleColor, vehicleTracksGeoJSON, vehicleTrackLabelsGeoJSON, TRACK_ELEVATION_SAMPLES, isVehicleDisconnected, isVehicleSelectable, vehicleStatus, vehicleStatusMapColor, vehicleStatusTextColor } from './mapUtils.js'
import { vehicleControlGroupDigits } from './squadUtils.js'
import { STYLE_URLS, applyBasemapConfig, applySatelliteLayerVisibility } from './mapStyles.js'
import { displayName } from './vehicleNames.js'
import MapUnavailable from './MapUnavailable.jsx'
import { cameraRay, setpointPath, cameraLabel, setpointLabel, vehicleCameraGeoJSON, vehicleSetpointGeoJSON, vehicleFootprintGeoJSON } from './mapOverlays.js'
import { hasMapboxToken } from './mapboxToken.js'
import { classColor, pinOpacity, pinCardHTML } from './detectionPins.js'

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// No ring/badge here anymore -- the whole marker element rotates with
// `rotation: v.bearing` below, so anything drawn on it would visibly spin
// as the vehicle turns. Status is carried by fill color alone, which
// rotating doesn't affect. Quick-squad membership moved to the popup
// label instead (see the .setHTML call below).
//
// Dimming for offline vehicles is applied to an inner <g>, never to the
// outer <svg> returned here. mapboxgl.Marker takes this exact element as
// `this._element` and, because this map has terrain enabled, overwrites
// `this._element.style.opacity` itself every render frame to fade markers
// occluded behind terrain -- fighting any opacity we set directly on it
// and producing a periodic flash for markers near an occlusion boundary
// (stationary/offline markers are the ones likely to sit still on such a
// boundary). Mapbox never reaches into a nested element, so opacity set
// there is untouched by that logic.
// A detection pin's tag: a dot and the class, bordered in the class's colour.
// As with the vehicle marker, its fade goes on an inner element, since
// mapbox sets the marker element's own opacity for terrain occlusion.
function createPinTagElement(cls) {
  const el = document.createElement('div');
  const tag = document.createElement('div');
  const color = classColor(cls);
  Object.assign(tag.style, {
    display: 'flex', alignItems: 'center', gap: '3px', padding: '1px 6px 1px 3px',
    background: 'rgba(20,22,28,0.85)', border: `2px solid ${color}`, borderRadius: '10px',
    color: '#fff', fontSize: '11px', whiteSpace: 'nowrap', cursor: 'default',
  });
  const dot = document.createElement('span');
  Object.assign(dot.style, { width: '8px', height: '8px', borderRadius: '50%', background: color });
  const label = document.createElement('span');
  label.textContent = cls;
  tag.append(dot, label);
  el.appendChild(tag);
  return el;
}

function createVehicleMarkerElement(color, opacity = 1) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  el.setAttribute('width', '34');
  el.setAttribute('height', '34');
  el.setAttribute('viewBox', '0 0 34 34');

  const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  group.style.opacity = String(opacity);

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M17 6 L26 27 L17 22 L8 27 Z');
  // Assigned via style rather than the fill attribute so the CSS
  // var(--...) status color resolves against the page's theme tokens.
  path.style.fill = color;
  path.setAttribute('stroke', '#ffffff');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('stroke-linejoin', 'round');
  group.appendChild(path);
  el.appendChild(group);

  return el;
}

// The vehicle's shadow: a dim dot on the ground under its marker, which
// floats at the vehicle's altitude, so where it is over the ground still
// reads when the map is pitched. Opacity is on the inner circle, for the
// same reason as in createVehicleMarkerElement.
function createVehicleShadowElement(opacity = 1) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  el.setAttribute('width', '14');
  el.setAttribute('height', '14');
  el.setAttribute('viewBox', '0 0 14 14');
  const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  circle.setAttribute('cx', '7');
  circle.setAttribute('cy', '7');
  circle.setAttribute('r', '6');
  circle.setAttribute('fill', '#000000');
  circle.setAttribute('stroke', '#ffffff');
  circle.setAttribute('stroke-width', '1');
  circle.style.opacity = String(0.45 * opacity);
  el.appendChild(circle);
  return el;
}

// A small label chip beside a map overlay marker, with one line per entry of
// lines ([text, color]).
function overlayLabel(lines) {
  const label = document.createElement('div');
  Object.assign(label.style, {
    position: 'absolute', left: '100%', top: '50%', transform: 'translate(6px, -50%)',
    padding: '2px 6px', borderRadius: '4px', background: 'rgba(17,24,39,0.85)',
    fontFamily: 'monospace', fontSize: '11px', whiteSpace: 'nowrap', pointerEvents: 'none',
  });
  for (const [text, color] of lines) {
    const line = document.createElement('div');
    line.textContent = text;
    line.style.color = color;
    label.appendChild(line);
  }
  return label;
}

// An overlay marker: a ring of radius r in color with a black halo, plus
// marks (extra SVG elements, e.g. ticks or a centre dot) and a label chip.
function createOverlayMarkerElement(color, r, marks, lines) {
  const size = 2 * r + 10;
  const el = document.createElement('div');
  Object.assign(el.style, { width: `${size}px`, height: `${size}px`, pointerEvents: 'none' });
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('viewBox', `${-size / 2} ${-size / 2} ${size} ${size}`);
  svg.setAttribute('aria-hidden', 'true');
  const add = (tag, attrs) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    svg.appendChild(node);
  };
  add('circle', { r, fill: 'none', stroke: '#000', 'stroke-width': 4, opacity: 0.5 });
  add('circle', { r, fill: 'none', stroke: color, 'stroke-width': 2 });
  for (const [tag, attrs] of marks) add(tag, attrs);
  el.appendChild(svg);
  el.appendChild(overlayLabel(lines));
  return el;
}

// Where the camera's centre meets the ground: a crosshair with its label.
function createCameraHitElement(color, label) {
  const tick = (x1, y1, x2, y2) => ['line', { x1, y1, x2, y2, stroke: color, 'stroke-width': 2 }];
  return createOverlayMarkerElement(color, 6,
    [tick(-10, 0, -7, 0), tick(7, 0, 10, 0), tick(0, -10, 0, -7), tick(0, 7, 0, 10)],
    [[label, '#fcd34d']]);
}

// The setpoint: a ring with a centre dot in the vehicle's color, labelled.
function createSetpointElement(color, label) {
  return createOverlayMarkerElement(color, 11,
    [['circle', { r: 3, fill: color, stroke: '#fff', 'stroke-width': 1 }]],
    [['SETPOINT', '#86efac'], [label, '#f3f4f6']]);
}

// The line layers from each vehicle to where its camera points (see
// vehicleCameraGeoJSON) and to its setpoint (see vehicleSetpointGeoJSON).
const CAMERA_LINES = 'vehicle-camera-lines';
// The patch of ground each camera sees (see vehicleFootprintGeoJSON), as a
// fill and its outline, draped on the terrain
const CAMERA_FOOTPRINTS = 'vehicle-camera-footprints';
const CAMERA_FOOTPRINT_OUTLINES = 'vehicle-camera-footprint-outlines';
const SETPOINT_LINES = 'vehicle-setpoint-lines';

// The line layer of where each vehicle has been (see vehicleTracksGeoJSON),
// and the layer of its name along it (see vehicleTrackLabelsGeoJSON).
const TRACKS = 'vehicle-tracks';
const TRACK_LABELS = 'vehicle-track-labels';
const NO_FEATURES = { type: 'FeatureCollection', features: [] };
// Meters between a track's name labels
const TRACK_LABEL_SPACING_M = 50;

// The value of a CSS color variable of the page's theme, or fallback if it
// has none: map paint can't read var(--...) itself.
function themeColor(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

// A paint color by the feature's status, matching the vehicle markers.
function statusColor() {
  return ['match', ['get', 'status'],
    'offline', themeColor('--red-500', '#ef4444'),
    'selected', themeColor('--green-500', '#22c55e'),
    themeColor('--blue-500', '#3b82f6')];
}

// Adds the tracks' sources and layers to map, which setStyle removes, so
// this runs on every style.load.
function addTrackLayers(map, tracks, labels) {
  if (map.getSource(TRACKS)) return;
  // lineMetrics gives line-progress, which picks each spot's altitude
  map.addSource(TRACKS, { type: 'geojson', data: tracks, lineMetrics: true });
  map.addLayer({
    id: TRACKS,
    type: 'line',
    source: TRACKS,
    layout: {
      'line-join': 'round',
      'line-cap': 'round',
      // Drawn at the altitude flown, above the ground below it, rather than
      // draped on the ground, where 3D buildings hide it (experimental in
      // mapbox-gl 3.x)
      'line-elevation-reference': 'ground',
      'line-z-offset': ['at-interpolated',
        ['*', ['line-progress'], TRACK_ELEVATION_SAMPLES - 1], ['get', 'elevation']],
    },
    paint: {
      'line-color': statusColor(),
      'line-width': 3,
      'line-opacity': 0.8,
      // Parts behind buildings show faded, not hidden
      'line-occlusion-opacity': 0.35,
      // Full brightness under the Standard style's dusk light preset
      'line-emissive-strength': 1,
    },
  });
  map.addSource(TRACK_LABELS, { type: 'geojson', data: labels });
  map.addLayer({
    id: TRACK_LABELS,
    type: 'symbol',
    source: TRACK_LABELS,
    layout: {
      'text-field': ['get', 'label'],
      'text-size': 12,
      'text-anchor': 'bottom',
      'text-offset': [0, -0.3],
      // Lifted to the track's altitude there (experimental)
      'symbol-z-elevate': true,
    },
    paint: {
      'symbol-z-offset': ['get', 'alt'],
      'text-color': statusColor(),
      'text-halo-color': '#000000',
      'text-halo-width': 1.5,
      'text-emissive-strength': 1,
      'text-occlusion-opacity': 0.5,
    },
  });
}

// Adds the camera and setpoint lines' sources and layers to map, which
// setStyle removes, so this runs on every style.load. Like the tracks, both
// are drawn in the air: each feature's elevation is its altitude at its two
// ends, indexed by line-progress.
function addOverlayLayers(map, cameraLines, setpointLines, footprints) {
  if (map.getSource(CAMERA_LINES)) return;
  // First, so the lines draw over it
  map.addSource(CAMERA_FOOTPRINTS, { type: 'geojson', data: footprints });
  map.addLayer({
    id: CAMERA_FOOTPRINTS, type: 'fill', source: CAMERA_FOOTPRINTS,
    paint: { 'fill-color': cameraColor(), 'fill-opacity': 0.15, 'fill-emissive-strength': 1 },
  });
  map.addLayer({
    id: CAMERA_FOOTPRINT_OUTLINES, type: 'line', source: CAMERA_FOOTPRINTS,
    layout: { 'line-join': 'round' },
    paint: { 'line-color': cameraColor(), 'line-width': 1.5, 'line-opacity': 0.7, 'line-emissive-strength': 1 },
  });
  const layout = {
    'line-join': 'round',
    'line-cap': 'round',
    'line-elevation-reference': 'ground',
    'line-z-offset': ['at-interpolated', ['*', ['line-progress'], 1], ['get', 'elevation']],
  };
  const paint = { 'line-occlusion-opacity': 0.35, 'line-emissive-strength': 1 };
  map.addSource(CAMERA_LINES, { type: 'geojson', data: cameraLines, lineMetrics: true });
  map.addLayer({
    id: CAMERA_LINES, type: 'line', source: CAMERA_LINES, layout,
    paint: { ...paint, 'line-color': cameraColor(), 'line-width': 2.5 },
  });
  map.addSource(SETPOINT_LINES, { type: 'geojson', data: setpointLines, lineMetrics: true });
  map.addLayer({
    id: SETPOINT_LINES, type: 'line', source: SETPOINT_LINES, layout,
    paint: { ...paint, 'line-color': statusColor(), 'line-width': 3, 'line-dasharray': [0, 2] },
  });
}

// The camera line's color: the theme's accent.
function cameraColor() {
  return themeColor('--primary-color', '#f59e0b');
}

function MapboxMap({ selectedVehicle, vehicles, mapPanelSize, tracking, detectedObjects, mapHeight, squadList, onToggleVehicle, controlGroups, mapStyle, pins, expiryS }) {
  const mapRef = useRef()
  // The detection pins' markers by pin id, updated in place as pins change
  const pinMarkersRef = useRef(new Map());
  const mapContainerRef = useRef()
  const markerRefs = useRef([]); // To store references to all markers
  const isFirstStyleEffect = useRef(true);
  // The latest tracks and their labels, for the layers to start from after a
  // style change
  const tracksRef = useRef(NO_FEATURES);
  const trackLabelsRef = useRef(NO_FEATURES);
  // The latest camera and setpoint lines, likewise
  const cameraLinesRef = useRef(NO_FEATURES);
  const setpointLinesRef = useRef(NO_FEATURES);
  const footprintsRef = useRef(NO_FEATURES);
  useEffect(() => {
    mapRef.current = new mapboxgl.Map({
      container: mapContainerRef.current,
      style: 'mapbox://styles/mapbox/standard',
      center: [-79.94299, 40.44353],
      zoom: 13.03,
    });

    mapRef.current.on('load', () => {
      mapRef.current.addControl(new mapboxgl.NavigationControl());
    });

    // DEM/terrain and the basemap declutter config (dusk lighting,
    // decluttered labels/3D objects for Standard, hidden POI/place layers
    // for the classic satellite style -- see mapStyles.js) don't survive a
    // setStyle call (see the mapStyle effect below), so these are
    // re-applied on every style.load, not just the first -- unlike the
    // NavigationControl above, which is added once. The Map constructor's
    // `config` option silently fails to apply against the Standard style,
    // which is why this happens imperatively here instead (see
    // applyBasemapConfig's comment in mapStyles.js).
    mapRef.current.on('style.load', () => {
      mapRef.current.addSource('mapbox-dem', {
        type: 'raster-dem',
        url: 'mapbox://mapbox.mapbox-terrain-dem-v1',
        tileSize: 512,
        maxzoom: 14
      });
      mapRef.current.setTerrain({ source: 'mapbox-dem', exaggeration: 1.0 });
      applyBasemapConfig(mapRef.current);
      mapRef.current.setConfigProperty('basemap', 'lightPreset', 'dusk');
      mapRef.current.setConfigProperty('basemap', 'font', 'Montserrat');
      applySatelliteLayerVisibility(mapRef.current);
      addTrackLayers(mapRef.current, tracksRef.current, trackLabelsRef.current);
      addOverlayLayers(mapRef.current, cameraLinesRef.current, setpointLinesRef.current, footprintsRef.current);
    });

    // Add a small delay to ensure container is fully rendered
    const timer = setTimeout(() => {
      if (mapRef.current) {
        mapRef.current.resize();
      }
    }, 100);

    // The map container's width changes whenever the squad sidebar
    // collapses/expands (or the window resizes). Mapbox GL doesn't detect
    // that on its own -- it only repaints the canvas at whatever size it
    // measured on creation -- so without this the map stays the old size
    // and the freed-up space just shows as empty grey padding.
    const resizeObserver = new ResizeObserver(() => {
      if (mapRef.current) {
        mapRef.current.resize();
      }
    });
    resizeObserver.observe(mapContainerRef.current);

    return () => {
      clearTimeout(timer);
      resizeObserver.disconnect();
      mapRef.current.remove();
    }
  }, []);

  useEffect(() => {
    if (!mapRef.current) return;
    if (isFirstStyleEffect.current) { isFirstStyleEffect.current = false; return; }
    mapRef.current.setStyle(STYLE_URLS[mapStyle]);
  }, [mapStyle]);

  // Handle drawer width changes
  useEffect(() => {
    if (mapRef.current) {
      // Small delay to ensure smooth transition
      const resizeTimer = setTimeout(() => {
        mapRef.current.resize();
      }, 100);

      return () => clearTimeout(resizeTimer);
    }
  }, [mapPanelSize]);


  useEffect(() => {

    markerRefs.current.forEach(marker => marker.remove());
    markerRefs.current = [];
    tracksRef.current = vehicleTracksGeoJSON(vehicles, squadList);
    trackLabelsRef.current = vehicleTrackLabelsGeoJSON(vehicles, squadList, TRACK_LABEL_SPACING_M);
    mapRef.current.getSource(TRACKS)?.setData(tracksRef.current);
    mapRef.current.getSource(TRACK_LABELS)?.setData(trackLabelsRef.current);
    cameraLinesRef.current = vehicleCameraGeoJSON(vehicles, squadList);
    setpointLinesRef.current = vehicleSetpointGeoJSON(vehicles, squadList);
    mapRef.current.getSource(CAMERA_LINES)?.setData(cameraLinesRef.current);
    mapRef.current.getSource(SETPOINT_LINES)?.setData(setpointLinesRef.current);
    footprintsRef.current = vehicleFootprintGeoJSON(vehicles);
    mapRef.current.getSource(CAMERA_FOOTPRINTS)?.setData(footprintsRef.current);
    vehicles.forEach(v => {
      const isSelected = !!(squadList && squadList.includes(v.name));
      const status = vehicleStatus(isVehicleDisconnected(v), isSelected);
      const groupDigits = vehicleControlGroupDigits(controlGroups ?? {}, v.name);
      const chipsHtml = groupDigits.map((d) => `<span class="squad-chip">${d}</span>`).join('');
      const offline = status === 'offline';
      // The overlays' markers first, so the vehicle's draws over them
      const ray = cameraRay(v);
      if (ray && !ray.clamped) {
        markerRefs.current.push(new mapboxgl.Marker({ element: createCameraHitElement(cameraColor(), cameraLabel(ray, v.camera.pitch)) })
          .setLngLat(ray.end)
          .addTo(mapRef.current));
      }
      // Only a selected vehicle's, as with its line (vehicleSetpointGeoJSON)
      const path = isSelected ? setpointPath(v) : null;
      if (path) {
        markerRefs.current.push(new mapboxgl.Marker({ element: createSetpointElement(vehicleStatusMapColor(status), setpointLabel(path, v.setpoint.alt)), altitude: v.setpoint.alt })
          .setLngLat([v.setpoint.long, v.setpoint.lat])
          .addTo(mapRef.current));
      }
      // Shadow first, so the marker draws over it when they meet on the ground
      if (v.current.alt > 0) {
        markerRefs.current.push(new mapboxgl.Marker({ element: createVehicleShadowElement(offline ? 0.6 : 1), pitchAlignment: 'map' })
          .setLngLat([v.current.long, v.current.lat])
          .addTo(mapRef.current));
      }
      // At the vehicle's altitude above the ground under it, like the end of
      // its track (line-elevation-reference 'ground'); its label rides with it
      let marker = new mapboxgl.Marker({ element: createVehicleMarkerElement(vehicleStatusMapColor(status), offline ? 0.6 : 1), rotation: v.bearing, rotationAlignment: 'map', altitude: v.current.alt })
        .setLngLat([v.current.long, v.current.lat])
        .setPopup(
          new mapboxgl.Popup({ offset: 20, anchor: 'top', focusAfterOpen: false, closeButton: false, closeOnClick: false, className: 'vehicle-label-popup' })
            .setHTML(`<strong title="${escapeHtml(v.name)}" style="color:${vehicleStatusTextColor(status)}">${chipsHtml}${escapeHtml(displayName(v.name))}<br>${v.current.alt.toFixed(2)} m</strong>`)
        )
        .addTo(mapRef.current);
      marker.togglePopup();
      const markerDiv = marker.getElement();

      // A vehicle with no data can still be selected, to hold it
      if (onToggleVehicle && isVehicleSelectable(v)) {
        markerDiv.style.cursor = 'pointer';
        markerDiv.addEventListener('click', () => onToggleVehicle(v.name));
      } else if (onToggleVehicle) {
        markerDiv.style.cursor = 'not-allowed';
      }

      if (tracking && v.name === selectedVehicle) {
        mapRef.current.flyTo({
          center: [v.current.long, v.current.lat],
          //zoom: 18.03,
          essential: true, // this animation is considered essential with respect to prefers-reduced-motion
        });
      }
      markerRefs.current.push(marker);
    });

    if (detectedObjects != null) {
      detectedObjects.forEach(d => {
        // Create the SVG element
        const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        el.setAttribute('width', '16');
        el.setAttribute('height', '16');
        el.setAttribute('viewBox', '0 0 16 16');

        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('cx', '8');
        circle.setAttribute('cy', '8');
        circle.setAttribute('r', '7');
        circle.setAttribute('fill', vehicleColor(d.cls));
        circle.setAttribute('stroke', '#fff');
        circle.setAttribute('stroke-width', '2');

        el.appendChild(circle);

        let marker = new mapboxgl.Marker({ element: el })
          .setLngLat([d.longitude, d.latitude])
          .setPopup(new mapboxgl.Popup({ focusAfterOpen: false }).setHTML(`<strong style="color:black">${d.id} (${d.confidence.toFixed(2) * 100}%)</strong><img src="${d.link}" style="width:100%;height:auto;margin-top:5px;" />`))
          .addTo(mapRef.current);
        const markerDiv = marker.getElement();

        markerDiv.addEventListener('mouseenter', () => marker.togglePopup());
        markerDiv.addEventListener('mouseleave', () => marker.togglePopup());
        markerRefs.current.push(marker);
      });
    }

  }, [vehicles, detectedObjects, squadList, controlGroups]);

  // Detection pins: tags on the ground, with a card on hover. Kept apart from
  // the vehicle markers so they aren't rebuilt on every vehicle update.
  useEffect(() => {
    if (!mapRef.current) return;
    const markers = pinMarkersRef.current;
    const seen = new Set();
    for (const pin of pins ?? []) {
      seen.add(pin.id);
      let entry = markers.get(pin.id);
      if (!entry) {
        const popup = new mapboxgl.Popup({ offset: 14, focusAfterOpen: false, closeButton: false, closeOnClick: false, className: 'pin-card-popup' });
        const marker = new mapboxgl.Marker({ element: createPinTagElement(pin.class) })
          .setLngLat([pin.long, pin.lat])
          .setPopup(popup)
          .addTo(mapRef.current);
        const el = marker.getElement();
        el.addEventListener('mouseenter', () => { if (!popup.isOpen()) marker.togglePopup(); });
        el.addEventListener('mouseleave', () => { if (popup.isOpen()) marker.togglePopup(); });
        entry = { marker, popup };
        markers.set(pin.id, entry);
      }
      entry.marker.setLngLat([pin.long, pin.lat]);
      entry.popup.setHTML(pinCardHTML(pin));
      entry.marker.getElement().firstChild.style.opacity = String(pinOpacity(pin.last_seen_s, expiryS));
    }
    for (const [id, entry] of markers) {
      if (!seen.has(id)) {
        entry.marker.remove();
        markers.delete(id);
      }
    }
  }, [pins, expiryS]);

  useEffect(() => {
    const markers = pinMarkersRef.current;
    return () => {
      for (const entry of markers.values()) entry.marker.remove();
      markers.clear();
    };
  }, []);

  useEffect(() => {
    let v = vehicles.find(v => v.name === selectedVehicle);
    if (v) {
      mapRef.current.flyTo({
        center: [v.current.long, v.current.lat],
        //zoom: 18.03,
        essential: true, // this animation is considered essential with respect to prefers-reduced-motion
      });
    }


  }, [selectedVehicle]);

  return (
    <div id='map-container' ref={mapContainerRef} style={{ width: '100%', height: mapHeight || '20rem' }} />
  )
}

function Mapbox(props) {
  return hasMapboxToken() ? <MapboxMap {...props} /> : <MapUnavailable height={props.mapHeight} />
}

export default Mapbox
