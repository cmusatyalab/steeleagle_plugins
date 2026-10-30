import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Dropdown } from 'primereact/dropdown';
import { useVehicleVideo } from './useVehicleVideo.js';
import { createCatalogLoader } from './flowCatalog.js';
import { RESULT_TTL_MS, detectionBoxes, resultEngineOptions } from './resultView.js';

const fill = { position: 'absolute', inset: 0, width: '100%', height: '100%' };

// The detections of `result` as boxes over a video of w x h pixels. The SVG
// scales like the video's object-fit: contain, so the boxes line up with it.
function DetectionOverlay({ result, w, h }) {
  const boxes = detectionBoxes(result, w, h);
  const fontSize = Math.max(h * 0.055, 12);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="xMidYMid meet" style={{ ...fill, pointerEvents: 'none' }}>
      {boxes.map((b, i) => (
        <g key={i}>
          <rect x={b.x} y={b.y} width={b.width} height={b.height} fill="none" stroke={b.color}
            strokeWidth={2} vectorEffect="non-scaling-stroke" />
          <text x={b.x + 2} y={b.y > fontSize ? b.y - 3 : b.y + fontSize} fill={b.color} fontSize={fontSize}
            fontFamily="monospace" stroke="#000" strokeWidth={fontSize * 0.15} paintOrder="stroke">{b.label}</text>
        </g>
      ))}
    </svg>
  );
}

// The vehicle's live video, with a dropdown over it to pick an engine on the
// GCS's machine whose results are drawn on it: detections as boxes, any other
// result as JSON along the bottom. The engine is the caller's state, so it
// outlives the panel.
function VehicleVideoPanel({ vehicle, engine, setEngine, height, noVideoLabel }) {
  // The latest result of the engine, about the camera shown
  const [result, setResult] = useState(null);
  const [catalog, setCatalog] = useState({ daemons: [], unavailable: [], error: null });
  const [size, setSize] = useState(null);
  const videoRef = useRef(null);

  const onResult = useCallback((message) => {
    if (message.engine === engine) setResult(message);
  }, [engine]);
  const video = useVehicleVideo(vehicle || null, engine, onResult);

  useEffect(() => {
    if (videoRef.current) videoRef.current.srcObject = video.stream;
  }, [video.stream]);

  // A result is cleared once no newer one comes for a while
  useEffect(() => {
    if (!result) return undefined;
    const timer = setTimeout(() => setResult(null), RESULT_TTL_MS);
    return () => clearTimeout(timer);
  }, [result]);

  const loadCatalog = useMemo(() => createCatalogLoader(setCatalog), []);
  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);
  const options = useMemo(() => resultEngineOptions(catalog, engine), [catalog, engine]);

  const onVideoSize = (e) => {
    const { videoWidth: w, videoHeight: h } = e.currentTarget;
    if (w > 0 && h > 0) setSize({ w, h });
  };

  const live = video.status === 'live' || video.status === 'stale';
  const shown = live && result && result.vehicle === vehicle ? result : null;
  const emptyMessage = catalog.error
    ? `Engines unavailable: ${catalog.error}`
    : 'No engine on this machine produces detection, classification, guidance or position results';

  return (
    <div style={{ height, backgroundColor: '#000', position: 'relative', overflow: 'hidden' }}>
      {video.stream && live && (
        <video ref={videoRef} autoPlay muted playsInline onLoadedMetadata={onVideoSize} onResize={onVideoSize}
          style={{ ...fill, objectFit: 'contain' }} />
      )}
      {shown && size && shown.result_type === 'DetectionResult' && (
        <DetectionOverlay result={shown.result} w={size.w} h={size.h} />
      )}
      {shown && shown.result_type !== 'DetectionResult' && (
        <pre style={{ position: 'absolute', left: 0, right: 0, bottom: 0, margin: 0, padding: '0.5rem', color: '#fff',
          background: 'rgba(0,0,0,0.6)', fontSize: '0.8rem', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
          maxHeight: '40%', overflowY: 'auto' }}>
          {JSON.stringify(shown.result)}
        </pre>
      )}
      {video.status !== 'live' && (
        <div style={{ position: 'absolute', inset: 0 }}>
          {video.status !== 'stale' && <img src="nostream.png" alt="" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />}
          <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, padding: '0.5rem', color: '#fff', background: 'rgba(0,0,0,0.6)', fontSize: '0.85rem' }}>
            {video.status === 'idle' ? 'Select a video feed' : video.status === 'connecting' ? 'Connecting to video…' : noVideoLabel}
          </div>
        </div>
      )}
      <div style={{ position: 'absolute', top: '0.5rem', left: '0.5rem', zIndex: 1 }}>
        <Dropdown value={engine} options={options} showClear placeholder="Engine results"
          onChange={(e) => {
            setEngine(e.value ?? null);
            setResult(null);
          }}
          onShow={loadCatalog} emptyMessage={emptyMessage}
          className="p-inputtext-sm" style={{ minWidth: '14rem', opacity: 0.9 }}
          aria-label="Engine whose results are shown on the video" />
      </div>
    </div>
  );
}

export default VehicleVideoPanel;
