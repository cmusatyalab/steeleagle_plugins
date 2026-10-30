import { useEffect, useState } from 'react';
import { getApiUrl } from './urls.js';
import { createVideoFeedTracker } from './videoFeeds.js';

// Whether each vehicle's video is reaching the GCS, as
// {vehicle: {live, ageMs}} (see createVideoFeedTracker), from one poll of
// /api/streams shared by every vehicle.
export function useVideoFeeds(pollMs = 1000) {
  const [feeds, setFeeds] = useState({});

  useEffect(() => {
    const tracker = createVideoFeedTracker();
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(getApiUrl('/api/streams'));
        if (!res.ok || cancelled) return;
        const streams = await res.json();
        if (!cancelled) setFeeds(tracker.observe(streams));
      } catch {
        // The GCS is unreachable; the rest of the UI says so
      }
    };
    poll();
    const timer = setInterval(poll, pollMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pollMs]);

  return feeds;
}
