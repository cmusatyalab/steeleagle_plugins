import { useEffect, useRef, useState } from 'react';
import { getApiUrl } from './urls.js';
import { createVideoSession, startStreamMonitor } from './webrtcVideo.js';

const IDLE = { stream: null, status: 'idle', error: null };

// Live video of `vehicle` (full vehicle name) over WebRTC. Polls /api/streams
// until the vehicle has a video producer, then connects; the peer connection
// is closed when the vehicle changes or the component unmounts.
//
// `engine` (a full engine service name, or null) is the engine whose results
// are wanted; each one, about the camera shown, is passed to `onResult` as the
// GCS sends it (rtc.DataMessage). Changing either doesn't reconnect.
export function useVehicleVideo(vehicle, engine = null, onResult = null) {
  const [state, setState] = useState(IDLE);
  const sessionRef = useRef(null);
  const engineRef = useRef(engine);
  const onResultRef = useRef(onResult);

  useEffect(() => {
    onResultRef.current = onResult;
  }, [onResult]);

  useEffect(() => {
    engineRef.current = engine;
    sessionRef.current?.setResultsEngine(engine);
  }, [engine]);

  useEffect(() => {
    if (!vehicle) {
      setState(IDLE);
      return undefined;
    }
    let cancelled = false;
    setState({ stream: null, status: 'waiting', error: null });

    const session = createVideoSession({
      fetchImpl: (...args) => fetch(...args),
      PeerConnection: RTCPeerConnection,
      onChange: (s) => {
        if (!cancelled) setState(s);
      },
      onResult: (message) => {
        if (!cancelled) onResultRef.current?.(message);
      },
    });
    session.setResultsEngine(engineRef.current);
    sessionRef.current = session;

    const stop = startStreamMonitor({
      vehicle,
      session,
      fetchStreams: async () => {
        const res = await fetch(getApiUrl('/api/streams'));
        return res.ok ? res.json() : null;
      },
    });

    return () => {
      cancelled = true;
      sessionRef.current = null;
      stop();
      session.close();
    };
  }, [vehicle]);

  return state;
}
