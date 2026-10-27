import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createJoystickSender, createSerialPoster, velocityFrom, MOVEMENT_KEYS } from './joystick.js';

const ZERO = { xvel: 0, yvel: 0, zvel: 0, angularvel: 0 };
const FWD = { xvel: 5, yvel: 0, zvel: 0, angularvel: 0 };

describe('velocityFrom', () => {
    it('maps held keys to velocities', () => {
        expect(velocityFrom({ keys: new Set(['KeyW', 'KeyD']), axes: {}, planar: 5, angular: 90 }))
            .toEqual({ xvel: 5, yvel: 5, zvel: 0, angularvel: 0 });
    });

    it('covers every movement key with its sign', () => {
        expect(MOVEMENT_KEYS.KeyS).toEqual({ axis: 'x', sign: -1 });
        expect(MOVEMENT_KEYS.KeyA).toEqual({ axis: 'y', sign: -1 });
        expect(MOVEMENT_KEYS.KeyI).toEqual({ axis: 'z', sign: 1 });
        expect(MOVEMENT_KEYS.KeyK).toEqual({ axis: 'z', sign: -1 });
        expect(MOVEMENT_KEYS.KeyL).toEqual({ axis: 'a', sign: 1 });
        expect(MOVEMENT_KEYS.KeyJ).toEqual({ axis: 'a', sign: -1 });
        expect(velocityFrom({ keys: new Set(['KeyJ', 'KeyK']), axes: {}, planar: 2, angular: 90 }))
            .toEqual({ xvel: 0, yvel: 0, zvel: -2, angularvel: -90 });
    });

    it('uses the gamepad axes when no key is held', () => {
        expect(velocityFrom({ keys: new Set(), axes: { 3: -0.5 }, planar: 5, angular: 90 }).xvel).toBe(2.5);
        expect(velocityFrom({ keys: new Set(), axes: { 0: 0.5, 1: 1, 2: 1 }, planar: 5, angular: 90 }))
            .toEqual({ xvel: 0, yvel: 5, zvel: -5, angularvel: 45 });
    });

    it('lets keys override axes', () => {
        expect(velocityFrom({ keys: new Set(['KeyW']), axes: { 2: 1 }, planar: 5, angular: 90 }))
            .toEqual(FWD);
    });

    it('returns plain zeros when idle, never -0 or NaN', () => {
        const v = velocityFrom({ keys: new Set(), axes: { index: -99, value: -99 }, planar: 5, angular: 90 });
        expect(v).toEqual(ZERO);
        expect(Object.is(v.xvel, 0)).toBe(true);
    });
});

describe('createJoystickSender', () => {
    let send;
    let sender;
    beforeEach(() => {
        vi.useFakeTimers();
        send = vi.fn();
        sender = createJoystickSender({ send, intervalMs: 150 });
    });
    afterEach(() => {
        sender.stop();
        vi.useRealTimers();
    });

    it('repeats while held', () => {
        sender.update(FWD);
        vi.advanceTimersByTime(450);
        expect(send).toHaveBeenCalledTimes(4);
        send.mock.calls.forEach(([v]) => expect(v.xvel).toBe(5));
    });

    it('does not restart the timer when the same value is re-applied', () => {
        sender.update(FWD);
        vi.advanceTimersByTime(100);
        sender.update({ ...FWD });
        vi.advanceTimersByTime(50);
        expect(send).toHaveBeenCalledTimes(2);
    });

    it('sends at once when the value changes while moving', () => {
        sender.update(FWD);
        sender.update({ ...FWD, yvel: 5 });
        expect(send).toHaveBeenCalledTimes(2);
        vi.advanceTimersByTime(150);
        expect(send).toHaveBeenLastCalledWith({ ...FWD, yvel: 5 });
    });

    it('sends one zero on release', () => {
        sender.update(FWD);
        vi.advanceTimersByTime(450);
        send.mockClear();
        sender.update(ZERO);
        vi.advanceTimersByTime(1000);
        expect(send).toHaveBeenCalledTimes(1);
        expect(send).toHaveBeenCalledWith(ZERO);
    });

    it('sends nothing when idle', () => {
        sender.update(ZERO);
        vi.advanceTimersByTime(1000);
        expect(send).not.toHaveBeenCalled();
    });

    it('stop while moving sends zero and clears the timer', () => {
        sender.update(FWD);
        send.mockClear();
        sender.stop();
        expect(send).toHaveBeenCalledTimes(1);
        expect(send).toHaveBeenCalledWith(ZERO);
        vi.advanceTimersByTime(1000);
        expect(send).toHaveBeenCalledTimes(1);
    });

    it('stop while idle sends nothing', () => {
        sender.stop();
        expect(send).not.toHaveBeenCalled();
    });

    it('cancel clears the timer and sends nothing, then stop is silent', () => {
        sender.update(FWD);
        send.mockClear();
        sender.cancel();
        expect(sender.isMoving()).toBe(false);
        vi.advanceTimersByTime(1000);
        sender.stop();
        expect(send).not.toHaveBeenCalled();
    });

    it('cancel while idle is harmless and the sender restarts afterwards', () => {
        sender.cancel();
        sender.update(FWD);
        expect(sender.isMoving()).toBe(true);
        expect(send).toHaveBeenCalledTimes(1);
    });

    it('forceStop while idle sends exactly one zero', () => {
        sender.forceStop();
        expect(send).toHaveBeenCalledTimes(1);
        expect(send).toHaveBeenCalledWith(ZERO);
        vi.advanceTimersByTime(1000);
        expect(send).toHaveBeenCalledTimes(1);
    });

    it('forceStop after cancel sends exactly one zero', () => {
        sender.update(FWD);
        sender.cancel();
        send.mockClear();
        sender.forceStop();
        expect(send).toHaveBeenCalledTimes(1);
        expect(send).toHaveBeenCalledWith(ZERO);
    });

    it('forceStop while moving sends one zero and clears the timer', () => {
        sender.update(FWD);
        send.mockClear();
        sender.forceStop();
        expect(sender.isMoving()).toBe(false);
        vi.advanceTimersByTime(1000);
        sender.stop();
        expect(send).toHaveBeenCalledTimes(1);
        expect(send).toHaveBeenCalledWith(ZERO);
    });

    it('can be restarted after stop', () => {
        sender.update(FWD);
        sender.stop();
        send.mockClear();
        sender.update(FWD);
        vi.advanceTimersByTime(150);
        expect(send).toHaveBeenCalledTimes(2);
    });
});

describe('createSerialPoster', () => {
    const deferred = () => {
        let resolve;
        const promise = new Promise((r) => { resolve = r; });
        return { promise, resolve };
    };
    const tick = () => new Promise((r) => setTimeout(r, 0));

    it('keeps one request in flight and collapses pending to the newest', async () => {
        const gates = [];
        const sent = [];
        const poster = createSerialPoster((v) => {
            sent.push(v);
            const d = deferred();
            gates.push(d);
            return d.promise;
        });
        poster.submit(1);
        poster.submit(2);
        poster.submit(3);
        poster.submit(4);
        expect(sent).toEqual([1]);
        gates[0].resolve();
        await tick();
        expect(sent).toEqual([1, 4]);
        gates[1].resolve();
        await tick();
        expect(sent).toEqual([1, 4]);
    });

    it('sends the last requested value last (release zero is never overtaken)', async () => {
        const gates = [];
        const sent = [];
        const poster = createSerialPoster((v) => {
            sent.push(v);
            const d = deferred();
            gates.push(d);
            return d.promise;
        });
        poster.submit('repeat');
        poster.submit('repeat2');
        poster.submit('zero');
        gates[0].resolve();
        await tick();
        gates[1].resolve();
        await tick();
        expect(sent).toEqual(['repeat', 'zero']);
    });

    it('preserves order across different keys', async () => {
        const gates = [];
        const sent = [];
        const poster = createSerialPoster((v) => {
            sent.push(v);
            const d = deferred();
            gates.push(d);
            return d.promise;
        });
        poster.submit('a1', 'A');
        poster.submit('zeroA', 'A');
        poster.submit('b1', 'B');
        poster.submit('b2', 'B');
        for (let i = 0; i < 4; i++) { gates[i]?.resolve(); await tick(); }
        expect(sent).toEqual(['a1', 'zeroA', 'b2']);
    });

    it('keeps draining after a failing post, and clear drops the queue', async () => {
        const sent = [];
        const gates = [];
        const poster = createSerialPoster((v) => {
            sent.push(v);
            const d = deferred();
            gates.push(d);
            return d.promise;
        });
        poster.submit(1, 'a');
        poster.submit(2, 'b');
        poster.clear();
        gates[0].resolve();
        await tick();
        expect(sent).toEqual([1]);
        poster.submit(3);
        expect(sent).toEqual([1, 3]);
        gates[1].resolve();
    });

    it('survives a rejecting post', async () => {
        const sent = [];
        const poster = createSerialPoster((v) => {
            sent.push(v);
            return Promise.reject(new Error('x'));
        });
        poster.submit(1);
        await tick();
        poster.submit(2);
        expect(sent).toEqual([1, 2]);
    });

    it('idle resolves immediately when nothing is in flight', async () => {
        const poster = createSerialPoster(() => Promise.resolve());
        await expect(poster.idle()).resolves.toBeUndefined();
    });

    it('idle resolves after the in-flight post settles', async () => {
        const d = deferred();
        const poster = createSerialPoster(() => d.promise);
        poster.submit(1);
        let done = false;
        poster.idle().then(() => { done = true; });
        await tick();
        expect(done).toBe(false);
        d.resolve();
        await tick();
        expect(done).toBe(true);
    });

    it('idle resolves even if the in-flight post rejects', async () => {
        const d = deferred();
        const poster = createSerialPoster(() => d.promise);
        poster.submit(1);
        let done = false;
        poster.idle().then(() => { done = true; });
        d.resolve(Promise.reject(new Error('x')));
        await tick();
        expect(done).toBe(true);
    });

    it('clear(key) drops only that key and keeps departed-vehicle zeros', async () => {
        const gates = [];
        const sent = [];
        const poster = createSerialPoster((v) => {
            sent.push(v);
            const d = deferred();
            gates.push(d);
            return d.promise;
        });
        poster.submit('first', 'cur');
        poster.submit('zeroLeft', 'left');
        poster.submit('repeat', 'cur');
        poster.clear('cur');
        for (let i = 0; i < 3; i++) { gates[i]?.resolve(); await tick(); }
        expect(sent).toEqual(['first', 'zeroLeft']);
    });
});
