import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { pickVideoProducer, connectVideo, createVideoSession, startStreamMonitor } from './webrtcVideo.js';

class FakePC {
    static instances = [];
    constructor() {
        this.closed = false;
        this.iceGatheringState = 'new';
        this.connectionState = 'new';
        this.listeners = {};
        this.transceivers = [];
        this.channels = [];
        this.remote = null;
        this.stream = { id: `stream-${FakePC.instances.length}` };
        FakePC.instances.push(this);
    }
    addTransceiver(kind, init) { this.transceivers.push({ kind, init }); }
    createDataChannel(label) { this.channels.push(label); }
    async createOffer() { return { type: 'offer', sdp: 'fake offer sdp' }; }
    async setLocalDescription(d) {
        this.localDescription = d;
        this.iceGatheringState = 'complete';
    }
    async setRemoteDescription(d) {
        this.remote = d;
        if (this.ontrack) this.ontrack({ streams: [this.stream] });
    }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] || []).filter(f => f !== fn); }
    fire(name) { (this.listeners[name] || []).forEach(f => f()); }
    close() { this.closed = true; }
}

const okFetch = () => vi.fn(async () => ({ ok: true, json: async () => ({ type: 'answer', sdp: 'answer sdp' }) }));

beforeEach(() => { vi.stubGlobal('window', { location: { protocol: 'http:', host: 'gcs.example:8002' } }); });
afterEach(() => { FakePC.instances = []; vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('pickVideoProducer', () => {
    const s = (producer, vehicle, kind, last_seen) => ({ producer, vehicle, kind, last_seen, viewers: 0 });
    it('ignores telemetry streams and other vehicles', () => {
        const streams = [
            s('a.tel', 'a', 'telemetry', '2026-01-01T00:00:09Z'),
            s('b.cam', 'b', 'video', '2026-01-01T00:00:09Z'),
        ];
        expect(pickVideoProducer(streams, 'a')).toBeNull();
    });
    it('picks the newest of two cameras', () => {
        const streams = [
            s('a.front', 'a', 'video', '2026-01-01T00:00:01Z'),
            s('a.down', 'a', 'video', '2026-01-01T00:00:05Z'),
        ];
        expect(pickVideoProducer(streams, 'a')).toBe('a.down');
    });
    it('returns null when there is none', () => {
        expect(pickVideoProducer([], 'a')).toBeNull();
        expect(pickVideoProducer(null, 'a')).toBeNull();
    });
});

describe('connectVideo', () => {
    it('posts the offer and applies the answer', async () => {
        const fetchImpl = okFetch();
        const { pc, stream } = await connectVideo({
            producer: '100.64.1.5.cassowary.front', fetchImpl, PeerConnection: FakePC,
        });
        const [url, init] = fetchImpl.mock.calls[0];
        expect(url).toContain('/api/webrtc/offer');
        expect(init.method).toBe('POST');
        expect(JSON.parse(init.body)).toEqual({ sdp: 'fake offer sdp', video: ['100.64.1.5.cassowary.front'] });
        expect(pc.remote).toEqual({ type: 'answer', sdp: 'answer sdp' });
        expect(pc.transceivers).toEqual([{ kind: 'video', init: { direction: 'recvonly' } }]);
        expect(pc.channels).toEqual(['gcs']);
        expect(stream).toBe(pc.stream);
    });

    it('rejects on HTTP error and closes the connection', async () => {
        const fetchImpl = vi.fn(async () => ({ ok: false, status: 400, text: async () => 'invalid offer' }));
        await expect(connectVideo({ producer: 'p', fetchImpl, PeerConnection: FakePC })).rejects.toThrow(/invalid offer/);
        expect(FakePC.instances[0].closed).toBe(true);
    });

    it('waits for ICE gathering to complete', async () => {
        class SlowPC extends FakePC {
            async setLocalDescription(d) { this.localDescription = d; }
        }
        const fetchImpl = okFetch();
        const p = connectVideo({ producer: 'p', fetchImpl, PeerConnection: SlowPC });
        await new Promise(r => setTimeout(r, 5));
        expect(fetchImpl).not.toHaveBeenCalled();
        const pc = FakePC.instances[0];
        pc.iceGatheringState = 'complete';
        pc.fire('icegatheringstatechange');
        await p;
        expect(fetchImpl).toHaveBeenCalled();
    });
});

describe('createVideoSession', () => {
    it('closes the previous connection and never shows the old stream', async () => {
        let releaseA;
        const fetchImpl = vi.fn(async (url, init) => {
            const body = JSON.parse(init.body);
            if (body.video[0] === 'A') await new Promise(r => { releaseA = r; });
            return { ok: true, json: async () => ({ type: 'answer', sdp: 'x' }) };
        });
        const changes = [];
        const session = createVideoSession({ fetchImpl, PeerConnection: FakePC, onChange: c => changes.push(c) });
        session.switchTo('A');
        await new Promise(r => setTimeout(r, 5));
        session.switchTo('B');
        await new Promise(r => setTimeout(r, 5));
        const [pcA, pcB] = FakePC.instances;
        expect(pcA.closed).toBe(true);
        releaseA();
        await new Promise(r => setTimeout(r, 5));
        expect(pcB.closed).toBe(false);
        expect(changes.some(c => c.stream === pcA.stream)).toBe(false);
        expect(changes[changes.length - 1]).toEqual({ stream: pcB.stream, status: 'live', error: null });
    });

    it('switchTo(null) closes and goes idle; close() closes', async () => {
        const changes = [];
        const session = createVideoSession({ fetchImpl: okFetch(), PeerConnection: FakePC, onChange: c => changes.push(c) });
        session.switchTo('A');
        await new Promise(r => setTimeout(r, 5));
        session.switchTo(null);
        expect(FakePC.instances[0].closed).toBe(true);
        expect(changes[changes.length - 1].status).toBe('idle');
        expect(changes[changes.length - 1].stream).toBeNull();
        session.switchTo('A');
        await new Promise(r => setTimeout(r, 5));
        session.close();
        expect(FakePC.instances[1].closed).toBe(true);
    });

    it('reconnects 2s after the connection fails, and not after close', async () => {
        vi.useFakeTimers();
        const changes = [];
        const session = createVideoSession({ fetchImpl: okFetch(), PeerConnection: FakePC, onChange: c => changes.push(c) });
        session.switchTo('A');
        await vi.advanceTimersByTimeAsync(0);
        const pc1 = FakePC.instances[0];
        pc1.connectionState = 'failed';
        pc1.fire('connectionstatechange');
        expect(pc1.closed).toBe(true);
        expect(changes[changes.length - 1].status).toBe('failed');
        await vi.advanceTimersByTimeAsync(1999);
        expect(FakePC.instances.length).toBe(1);
        await vi.advanceTimersByTimeAsync(2);
        expect(FakePC.instances.length).toBe(2);
        expect(changes[changes.length - 1].status).toBe('live');
        FakePC.instances[1].connectionState = 'disconnected';
        FakePC.instances[1].fire('connectionstatechange');
        session.close();
        await vi.advanceTimersByTimeAsync(5000);
        expect(FakePC.instances.length).toBe(2);
    });
});

describe('startStreamMonitor', () => {
    const entry = (producer, last_seen, vehicle = 'a') => ({ producer, vehicle, kind: 'video', last_seen, viewers: 0 });
    const setup = (getStreams) => {
        const session = { switchTo: vi.fn(), setStale: vi.fn() };
        const stop = startStreamMonitor({ vehicle: 'a', session, fetchStreams: async () => getStreams() });
        return { session, stop };
    };
    const lastStale = (session) => session.setStale.mock.calls.at(-1)?.[0];

    it('keeps live while last_seen advances, goes stale after 3s, recovers when it advances', async () => {
        vi.useFakeTimers();
        let n = 0;
        let frozen = false;
        const { session, stop } = setup(() => [entry('a.cam', `2026-01-01T00:00:${String(frozen ? 10 : n++).padStart(2, '0')}Z`)]);
        await vi.advanceTimersByTimeAsync(0);
        expect(session.switchTo).toHaveBeenCalledWith('a.cam');
        await vi.advanceTimersByTimeAsync(5000);
        expect(lastStale(session)).toBe(false);
        expect(session.switchTo).toHaveBeenCalledTimes(1);
        frozen = true;
        n = 10;
        await vi.advanceTimersByTimeAsync(1000);
        expect(lastStale(session)).toBe(false);
        await vi.advanceTimersByTimeAsync(3000);
        expect(lastStale(session)).toBe(true);
        frozen = false;
        n = 20;
        await vi.advanceTimersByTimeAsync(1000);
        expect(lastStale(session)).toBe(false);
        stop();
    });

    it('switches to a different newest producer', async () => {
        vi.useFakeTimers();
        let streams = [entry('a.front', '2026-01-01T00:00:01Z')];
        const { session, stop } = setup(() => streams);
        await vi.advanceTimersByTimeAsync(0);
        streams = [entry('a.front', '2026-01-01T00:00:01Z'), entry('a.down', '2026-01-01T00:00:05Z')];
        await vi.advanceTimersByTimeAsync(1000);
        expect(session.switchTo).toHaveBeenLastCalledWith('a.down');
        stop();
    });

    it('stops polling after stop()', async () => {
        vi.useFakeTimers();
        const get = vi.fn(() => []);
        const { stop } = setup(get);
        await vi.advanceTimersByTimeAsync(0);
        stop();
        const n = get.mock.calls.length;
        await vi.advanceTimersByTimeAsync(5000);
        expect(get.mock.calls.length).toBe(n);
    });
});

describe('createVideoSession staleness and closing', () => {
    it('reports stale then live again with the same stream, and resets on switch', async () => {
        const changes = [];
        const session = createVideoSession({ fetchImpl: okFetch(), PeerConnection: FakePC, onChange: c => changes.push(c) });
        session.setStale(true); // ignored: nothing live
        expect(changes).toEqual([]);
        session.switchTo('A');
        await new Promise(r => setTimeout(r, 5));
        const stream = FakePC.instances[0].stream;
        session.setStale(true);
        expect(changes.at(-1)).toEqual({ stream, status: 'stale', error: null });
        expect(FakePC.instances[0].closed).toBe(false);
        session.setStale(false);
        expect(changes.at(-1)).toEqual({ stream, status: 'live', error: null });
    });

    it('a pc closed while waiting for ICE rejects instead of hanging', async () => {
        class NeverPC extends FakePC {
            async setLocalDescription(d) { this.localDescription = d; }
        }
        const p = connectVideo({ producer: 'p', fetchImpl: okFetch(), PeerConnection: NeverPC });
        const assertion = expect(p).rejects.toThrow(/closed/);
        await new Promise(r => setTimeout(r, 5));
        FakePC.instances[0].close();
        await assertion;
    });
});
