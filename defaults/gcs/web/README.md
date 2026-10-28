The Mapbox token comes from the GCS's `mapbox` plugin arg (served at
`/api/config`), so run against a GCS (or `pixi run mock`, which passes
`$MAPBOX_TOKEN`).

# Run on localhost or bind to address
```
npm run dev
# or
npm run dev -- -- host <ip>
```
