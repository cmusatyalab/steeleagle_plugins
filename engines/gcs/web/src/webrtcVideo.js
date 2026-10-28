import { getApiUrl } from './urls.js';

export const RECONNECT_DELAY_MS = 2000;
const TRACK_TIMEOUT_MS = 10000;

// The newest-seen video stream of one vehicle, or null. `vehicle` is the full
// vehicle name, matching the `vehicle` field of /api/streams entries.
export function pickVideoProducer(streams, vehicle) {
  let best = null;
  let bestTime = -Infinity;
  for (const s of streams || []) {
    if (s.kind !== 'video' || s.vehicle !== vehicle) continue;
    const t = Date.parse(s.last_seen);
    const time = Number.isNaN(t) ? -Infinity : t;
    if (best === null || time > bestTime) {
      best = s.producer;
      bestTime = time;
    }
  }
  return best;
}

export const STALE_AFTER_MS = 3000;

// Tracks whether a producer's last_seen is still advancing. last_seen values
// are only compared with each other (never with the browser clock); only the
// time since the last advance is measured locally.
export function createStalenessTracker({ staleAfterMs = STALE_AFTER_MS, now = Date.now } = {}) {
  let last;
  let changedAt = now();
  return {
    // Feed the producer's latest last_seen (or undefined if it is missing);
    // returns true when it has not advanced for staleAfterMs.
    observe(lastSeen) {
      const t = lastSeen === undefined ? NaN : Date.parse(lastSeen);
      if (!Number.isNaN(t) && (last === undefined || t > last)) {
        last = t;
        changedAt = now();
      }
      return now() - changedAt >= staleAfterMs;
    },
  };
}

function waitForIceComplete(pc) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const onChange = () => {
      if (pc.iceGatheringState === 'complete') {
        pc.removeEventListener('icegatheringstatechange', onChange);
        resolve();
      }
    };
    pc.addEventListener('icegatheringstatechange', onChange);
  });
}

// Offers one receive-only video track for `producer` and resolves with the
// peer connection and its first remote stream. The peer connection is closed
// again if anything fails. `onPeer(pc)` is called as soon as the connection
// exists so a caller can close it while the handshake is still in flight.
export async function connectVideo({ producer, fetchImpl = fetch, PeerConnection = RTCPeerConnection, onPeer }) {
  const pc = new PeerConnection();
  if (onPeer) onPeer(pc);
  let timer = null;
  // pc.close() fires no events, so a close during the handshake would leave
  // the awaits below hanging; make it abort them instead.
  let abort;
  const aborted = new Promise((_, reject) => { abort = reject; });
  aborted.catch(() => {});
  const guard = (p) => Promise.race([p, aborted]);
  const origClose = pc.close.bind(pc);
  pc.close = () => {
    abort(new Error('connection closed'));
    origClose();
  };
  try {
    const gotStream = new Promise((resolve, reject) => {
      pc.ontrack = (ev) => {
        const stream = (ev.streams && ev.streams[0]) || null;
        if (stream) resolve(stream);
      };
      timer = setTimeout(() => reject(new Error('timed out waiting for video')), TRACK_TIMEOUT_MS);
    });
    gotStream.catch(() => {});
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.createDataChannel('gcs');
    await guard(pc.setLocalDescription(await guard(pc.createOffer())));
    await guard(waitForIceComplete(pc));
    const res = await guard(fetchImpl(getApiUrl('/api/webrtc/offer'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, video: [producer] }),
    }));
    if (!res.ok) {
      const text = await guard(res.text()).catch(() => '');
      throw new Error(text || `HTTP error! status: ${res.status}`);
    }
    const answer = await guard(res.json());
    await guard(pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp }));
    const stream = await guard(gotStream);
    return { pc, stream };
  } catch (e) {
    pc.close();
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// Holds at most one peer connection. onChange({stream, status, error}) only
// reports for the latest switchTo. A failed or dropped connection is retried
// after RECONNECT_DELAY_MS until switchTo/close is called.
export function createVideoSession({ fetchImpl, PeerConnection, onChange }) {
  let generation = 0;
  let pc = null;
  let retryTimer = null;
  let liveStream = null;
  let stale = false;

  const teardown = () => {
    generation += 1;
    clearTimeout(retryTimer);
    retryTimer = null;
    liveStream = null;
    stale = false;
    if (pc) {
      pc.close();
      pc = null;
    }
    return generation;
  };

  const connect = (producer, gen) => {
    onChange({ stream: null, status: 'connecting', error: null });
    const retry = (error) => {
      if (gen !== generation) return;
      if (pc) {
        pc.close();
        pc = null;
      }
      liveStream = null;
      stale = false;
      onChange({ stream: null, status: 'failed', error });
      retryTimer = setTimeout(() => {
        retryTimer = null;
        if (gen === generation) connect(producer, gen);
      }, RECONNECT_DELAY_MS);
    };
    connectVideo({
      producer, fetchImpl, PeerConnection,
      onPeer: (p) => {
        pc = p;
      },
    }).then(({ pc: connected, stream }) => {
      if (gen !== generation) {
        connected.close();
        return;
      }
      pc = connected;
      connected.addEventListener('connectionstatechange', () => {
        if (gen !== generation || pc !== connected) return;
        if (connected.connectionState === 'failed' || connected.connectionState === 'disconnected') {
          retry(`connection ${connected.connectionState}`);
        }
      });
      liveStream = stream;
      stale = false;
      onChange({ stream, status: 'live', error: null });
    }, (e) => {
      retry(e.message || String(e));
    });
  };

  return {
    switchTo(producer) {
      const gen = teardown();
      if (!producer) {
        onChange({ stream: null, status: 'idle', error: null });
        return;
      }
      connect(producer, gen);
    },
    // Marks the live feed as stalled (peer connection stays open) or
    // flowing again. Ignored unless a stream is live.
    setStale(value) {
      if (value === stale) return;
      stale = value;
      if (liveStream) onChange({ stream: liveStream, status: value ? 'stale' : 'live', error: null });
    },
    close() {
      teardown();
    },
  };
}

// Polls /api/streams for one vehicle while a feed is wanted: connects once a
// video producer appears, moves to a newer producer of the same vehicle, and
// flags the session stale when the current producer's last_seen stops
// advancing. Returns stop().
export function startStreamMonitor({ vehicle, session, fetchStreams, pollMs = 1000, staleAfterMs = STALE_AFTER_MS }) {
  let current = null;
  let tracker = null;
  let inFlight = false;
  let stopped = false;
  const poll = async () => {
    if (inFlight || stopped) return;
    inFlight = true;
    try {
      const streams = await fetchStreams();
      if (stopped || !streams) return;
      const best = pickVideoProducer(streams, vehicle);
      if (best && best !== current) {
        current = best;
        tracker = createStalenessTracker({ staleAfterMs });
        session.switchTo(best);
      }
      if (current) {
        const entry = streams.find((x) => x.producer === current);
        session.setStale(tracker.observe(entry ? entry.last_seen : undefined));
      }
    } catch {
      // keep polling
    } finally {
      inFlight = false;
    }
  };
  poll();
  const interval = setInterval(poll, pollMs);
  return () => {
    stopped = true;
    clearInterval(interval);
  };
}
