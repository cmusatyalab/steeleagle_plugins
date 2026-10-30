import { STALE_AFTER_MS } from './webrtcVideo.js';

// Tracks, for every vehicle, whether its video is reaching the GCS: whether
// the newest last_seen of its video streams on /api/streams has advanced in
// the last staleAfterMs. Like createStalenessTracker, last_seen values are
// only compared with each other; time since an advance is measured locally.
export function createVideoFeedTracker({ staleAfterMs = STALE_AFTER_MS, now = Date.now } = {}) {
  // Per vehicle: the newest last_seen seen, and when (local time) it was
  const seen = new Map();
  return {
    // Feed one /api/streams response; returns {vehicle: {live, ageMs}} for
    // every vehicle that has had a video stream. ageMs is the local time
    // since its video last advanced.
    observe(streams) {
      const newest = new Map();
      for (const s of streams || []) {
        if (s.kind !== 'video' || !s.vehicle) continue;
        const t = Date.parse(s.last_seen);
        if (Number.isNaN(t)) continue;
        if (!newest.has(s.vehicle) || t > newest.get(s.vehicle)) newest.set(s.vehicle, t);
      }
      const t0 = now();
      for (const [vehicle, t] of newest) {
        const prev = seen.get(vehicle);
        if (!prev || t > prev.last) seen.set(vehicle, { last: t, changedAt: t0 });
      }
      const out = {};
      for (const [vehicle, { changedAt }] of seen) {
        const ageMs = t0 - changedAt;
        out[vehicle] = { live: newest.has(vehicle) && ageMs < staleAfterMs, ageMs };
      }
      return out;
    },
  };
}
