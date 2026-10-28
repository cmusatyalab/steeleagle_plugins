import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import { PrimeReactProvider } from "primereact/api";
import mapboxgl from 'mapbox-gl'
import { getApiUrl } from './urls.js'

// The maps' Mapbox token is the GCS's mapbox plugin arg. It is set before the
// first render, since the maps are created as they mount; without it they
// still render, just with no basemap.
async function loadMapboxToken() {
  try {
    const resp = await fetch(getApiUrl('/api/config'))
    if (resp.ok) mapboxgl.accessToken = (await resp.json()).mapbox ?? ''
  } catch {
    // the GCS is unreachable; the rest of the UI says so
  }
}

loadMapboxToken().then(() => createRoot(document.getElementById('root')).render(
  <StrictMode>
    <PrimeReactProvider>
      <App />
    </PrimeReactProvider>
  </StrictMode>,
))
