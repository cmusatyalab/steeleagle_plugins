import { describe, it, expect } from 'vitest';
import { createVideoFeedTracker } from './videoFeeds.js';

const video = (vehicle, lastSeen, producer = `${vehicle}.cam`) => ({ kind: 'video', vehicle, producer, last_seen: lastSeen });

describe('createVideoFeedTracker', () => {
  function setup() {
    let t = 0;
    const tracker = createVideoFeedTracker({ staleAfterMs: 3000, now: () => t });
    return { tracker, advance: (ms) => { t += ms; } };
  }

  it('is live while last_seen keeps advancing', () => {
    const { tracker, advance } = setup();
    expect(tracker.observe([video('alpha', '2026-01-01T00:00:00Z')]).alpha.live).toBe(true);
    advance(2500);
    expect(tracker.observe([video('alpha', '2026-01-01T00:00:02Z')]).alpha).toEqual({ live: true, ageMs: 0 });
  });

  it('goes stale once last_seen stops advancing', () => {
    const { tracker, advance } = setup();
    tracker.observe([video('alpha', '2026-01-01T00:00:00Z')]);
    advance(3000);
    expect(tracker.observe([video('alpha', '2026-01-01T00:00:00Z')]).alpha).toEqual({ live: false, ageMs: 3000 });
  });

  it('is not live once the vehicle has no video stream listed', () => {
    const { tracker } = setup();
    tracker.observe([video('alpha', '2026-01-01T00:00:00Z')]);
    expect(tracker.observe([]).alpha.live).toBe(false);
  });

  it('uses the newest of a vehicle\'s cameras and ignores other kinds', () => {
    const { tracker, advance } = setup();
    tracker.observe([video('alpha', '2026-01-01T00:00:00Z', 'alpha.front'), video('alpha', '2026-01-01T00:00:00Z', 'alpha.down')]);
    advance(3000);
    const out = tracker.observe([
      video('alpha', '2026-01-01T00:00:00Z', 'alpha.front'),
      video('alpha', '2026-01-01T00:00:05Z', 'alpha.down'),
      { kind: 'telemetry', vehicle: 'bravo', producer: 'bravo.telemetry', last_seen: '2026-01-01T00:00:05Z' },
    ]);
    expect(out.alpha.live).toBe(true);
    expect(out.bravo).toBeUndefined();
  });
});
