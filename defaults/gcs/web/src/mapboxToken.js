import mapboxgl from 'mapbox-gl'

// mapbox-gl throws from the Map constructor when it has no access token,
// which, thrown from a mount effect, unmounts the whole app to a blank page.
// The token is set once in main.jsx before the first render, so the maps
// check it as they render and draw MapUnavailable instead.
export const hasMapboxToken = () => !!mapboxgl.accessToken;

export const MISSING_TOKEN_DETAIL =
  "No Mapbox access token is configured, so maps are disabled. Set the GCS service's mapbox arg under Orchestrate > Configure..., then restart the GCS.";
