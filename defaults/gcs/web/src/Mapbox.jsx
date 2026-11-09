import { useRef, useEffect } from 'react'
import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css';
import { vehicleColor, vehicleTracksGeoJSON, vehicleTrackLabelsGeoJSON, TRACK_ELEVATION_SAMPLES, isVehicleDisconnected, isVehicleSelectable, vehicleStatus, vehicleStatusMapColor, vehicleStatusTextColor } from './mapUtils.js'
import { vehicleControlGroupDigits } from './squadUtils.js'
import { STYLE_URLS, applyBasemapConfig, applySatelliteLayerVisibility } from './mapStyles.js'
import { displayName } from './vehicleNames.js'
import MapUnavailable from './MapUnavailable.jsx'
import { hasMapboxToken } from './mapboxToken.js'

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

function MapboxMap({ selectedVehicle, vehicles, mapPanelSize, tracking, detectedObjects, mapHeight, squadList, onToggleVehicle, controlGroups, mapStyle }) {
  const mapRef = useRef()
  const mapContainerRef = useRef()
  const markerRefs = useRef([]); // To store references to all markers
  const isFirstStyleEffect = useRef(true);
  // The latest tracks and their labels, for the layers to start from after a
  // style change
  const tracksRef = useRef(NO_FEATURES);
  const trackLabelsRef = useRef(NO_FEATURES);
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
    vehicles.forEach(v => {
      const isSelected = !!(squadList && squadList.includes(v.name));
      const status = vehicleStatus(isVehicleDisconnected(v), isSelected);
      const groupDigits = vehicleControlGroupDigits(controlGroups ?? {}, v.name);
      const chipsHtml = groupDigits.map((d) => `<span class="squad-chip">${d}</span>`).join('');
      const offline = status === 'offline';
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
