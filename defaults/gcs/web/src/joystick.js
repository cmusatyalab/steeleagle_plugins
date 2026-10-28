// Continuous manual control. The server treats each SetVelocityTarget as
// lasting until the next one (and the vehicle holds on its own 3 s after the
// last control call), so the UI must keep re-sending while a key or stick is
// held and must always send an explicit zero on release.

export const MOVEMENT_KEYS = {
    KeyW: { axis: 'x', sign: 1 },
    KeyS: { axis: 'x', sign: -1 },
    KeyD: { axis: 'y', sign: 1 },
    KeyA: { axis: 'y', sign: -1 },
    KeyI: { axis: 'z', sign: 1 },
    KeyK: { axis: 'z', sign: -1 },
    KeyL: { axis: 'a', sign: 1 },
    KeyJ: { axis: 'a', sign: -1 },
};

const ZERO = { xvel: 0, yvel: 0, zvel: 0, angularvel: 0 };

// `|| 0` folds -0 (and NaN) to plain 0.
const clean = (n) => n || 0;

export function velocityFrom({ keys, axes, planar, angular }) {
    const held = [...keys].filter((k) => MOVEMENT_KEYS[k]);
    if (held.length > 0) {
        const sum = { x: 0, y: 0, z: 0, a: 0 };
        held.forEach((k) => { sum[MOVEMENT_KEYS[k].axis] += MOVEMENT_KEYS[k].sign; });
        const clamp = (n) => Math.max(-1, Math.min(1, n));
        return {
            xvel: clean(planar * clamp(sum.x)),
            yvel: clean(planar * clamp(sum.y)),
            zvel: clean(planar * clamp(sum.z)),
            angularvel: clean(angular * clamp(sum.a)),
        };
    }
    const ax = (i) => Number(axes?.[i]) || 0;
    return {
        xvel: clean(-planar * ax(3)),
        yvel: clean(planar * ax(2)),
        zvel: clean(-planar * ax(1)),
        angularvel: clean(angular * ax(0)),
    };
}

const isZero = (v) => v.xvel === 0 && v.yvel === 0 && v.zvel === 0 && v.angularvel === 0;
const same = (a, b) => a.xvel === b.xvel && a.yvel === b.yvel && a.zvel === b.zvel && a.angularvel === b.angularvel;

export function createJoystickSender({ send, intervalMs = 150, setInterval: setIv = globalThis.setInterval, clearInterval: clearIv = globalThis.clearInterval }) {
    let timer = null;
    let latest = null;

    const halt = () => {
        if (timer !== null) {
            clearIv(timer);
            timer = null;
        }
    };

    return {
        update(v) {
            if (isZero(v)) {
                if (timer !== null) {
                    halt();
                    latest = null;
                    send({ ...ZERO });
                }
                return;
            }
            const changed = latest === null || !same(latest, v);
            latest = { ...v };
            if (changed) send({ ...latest });
            // Repeat the latest value; an unchanged update must not restart
            // the timer (keyboard auto-repeat would otherwise stall it).
            if (timer === null) {
                timer = setIv(() => send({ ...latest }), intervalMs);
            }
        },
        // Silently forget that we are moving: no zero is sent. For when another
        // command (land, RTH, takeoff) is about to take over the vehicle and a
        // trailing zero velocity could pre-empt it.
        cancel() {
            halt();
            latest = null;
        },
        isMoving() {
            return timer !== null;
        },
        stop() {
            if (timer !== null) {
                halt();
                latest = null;
                send({ ...ZERO });
            }
        },
        // Always send one zero, moving or not: the operator's explicit stop
        // must work even after a lost release zero or a cancel().
        forceStop() {
            halt();
            latest = null;
            send({ ...ZERO });
        },
    };
}

// Serializes posts: at most one request in flight. While one is in flight,
// later submissions queue up; consecutive submissions with the same key
// collapse to the newest, so a stale repeat never overtakes a later value
// (notably the release zero) and a slow link cannot build up a backlog.
// Submissions with different keys (e.g. different squads) keep their order.
export function createSerialPoster(post) {
    let inFlight = false;
    let queue = [];
    let waiters = [];

    const pump = async () => {
        inFlight = true;
        try {
            while (queue.length > 0) {
                const { value } = queue.shift();
                try {
                    await post(value);
                } catch {
                    // post() reports its own failures; keep draining.
                }
            }
        } finally {
            inFlight = false;
            const done = waiters;
            waiters = [];
            done.forEach((resolve) => resolve());
        }
    };

    return {
        submit(value, key = '') {
            const last = queue[queue.length - 1];
            if (last && last.key === key) {
                last.value = value;
            } else {
                queue.push({ value, key });
            }
            if (!inFlight) pump();
        },
        // Drop queued entries (not the request already in flight). With a key,
        // only entries submitted under that key are dropped.
        clear(key) {
            queue = key === undefined ? [] : queue.filter((q) => q.key !== key);
        },
        // Resolves once nothing is in flight (the queue is drained too, so
        // clear() first if pending entries should not be waited for).
        idle() {
            return inFlight ? new Promise((resolve) => { waiters.push(resolve); }) : Promise.resolve();
        },
    };
}
