import { useEffect, useState } from 'react';
import { getApiUrl } from './urls.js';
import { createVideoSession, startStreamMonitor } from './webrtcVideo.js';

const IDLE = { stream: null, status: 'idle', error: null };

// Live video of `vehicle` (full vehicle name) over WebRTC. Polls /api/streams
// until the vehicle has a video producer, then connects; the peer connection
// is closed when the vehicle changes or the component unmounts.
export function useVehicleVideo(vehicle) {
  const [state, setState] = useState(IDLE);

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
    });

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
      stop();
      session.close();
    };
  }, [vehicle]);

  return state;
}
