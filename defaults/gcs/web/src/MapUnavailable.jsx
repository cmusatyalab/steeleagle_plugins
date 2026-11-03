import { MISSING_TOKEN_DETAIL } from './mapboxToken.js'

// Drawn in place of a map when there is no Mapbox token (see mapboxToken.js)
function MapUnavailable({ height }) {
  return (
    <div className="flex flex-column align-items-center justify-content-center gap-2 p-4 text-center border-round"
      style={{ width: '100%', height: height || '20rem', backgroundColor: 'var(--surface-ground)', border: '1px dashed var(--surface-border)', color: 'var(--text-color-secondary)' }}>
      <i className="pi pi-map" style={{ fontSize: '2rem' }} />
      <strong>Map unavailable</strong>
      <span style={{ maxWidth: '28rem' }}>{MISSING_TOKEN_DETAIL}</span>
    </div>
  );
}

export default MapUnavailable
