// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import MetricsPage from './MetricsPage.jsx';
import { POLL_INTERVAL_MS } from './metricsView.js';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// React Flow measures its container; jsdom has no layout, so give it one.
class FakeResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
}

const VEHICLE = '100.101.84.40.fake-drone';

const okView = {
    vehicle: VEHICLE, window: '1m', state: 'ok',
    nodes: [
        { id: 'rgb', kind: 'producer', label: 'rgb', host: 'fermi', metrics: null, bottleneck: false },
        { id: 'enc', kind: 'engine', label: 'encoder', host: 'fermi', bottleneck: true,
          metrics: { p50_seconds: 0.082, p95_seconds: 0.093, wait_seconds: 0.018, inputs_per_second: 12.2, skipped_share: 0.59, busy_fraction: 0.98 } },
    ],
    edges: [{ from: 'rgb', to: 'enc' }],
    end_to_end: { p50_seconds: 0.144, p95_seconds: 0.181 },
};

function respondWith(view) {
    globalThis.fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => view }));
}

async function render(props = {}) {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const all = { vehicles: [{ name: VEHICLE }], theme: 'dark', active: true, ...props };
    await act(async () => root.render(<MetricsPage {...all} />));
    // Let the first fetch resolve and render.
    await act(async () => {});
    const rerender = async (more) => {
        await act(async () => root.render(<MetricsPage {...all} {...more} />));
        await act(async () => {});
    };
    return { container, root, rerender };
}

beforeEach(() => {
    globalThis.ResizeObserver = FakeResizeObserver;
});

afterEach(() => {
    document.body.innerHTML = '';
    vi.useRealTimers();
});

describe('MetricsPage', () => {
    it('shows the flow with numbers when state is ok', async () => {
        respondWith(okView);

        const { container } = await render();

        expect(container.textContent).toContain('82 ms');
        expect(container.textContent).toContain('fermi');
        expect(container.textContent).toContain('bottleneck');
        expect(container.textContent).toContain('Capture → GCS');
        expect(container.textContent).toContain('144 ms');
    });

    it('shows the flow and a message when Prometheus is unreachable', async () => {
        respondWith({ ...okView, state: 'prometheus_unreachable', detail: 'connection refused', end_to_end: null,
            nodes: okView.nodes.map((n) => ({ ...n, metrics: null, bottleneck: false })) });

        const { container } = await render();

        expect(container.textContent).toContain('Prometheus unreachable');
        expect(container.textContent).toContain('encoder');
        expect(container.textContent).toContain('no metrics');
    });

    it('says the vehicle runs no flow', async () => {
        respondWith({ vehicle: VEHICLE, window: '1m', state: 'no_flow', nodes: [], edges: [], end_to_end: null });

        const { container } = await render();

        expect(container.textContent).toContain('runs no flow');
    });

    it('changing the window refetches with it', async () => {
        respondWith(okView);
        const { container } = await render();

        const fiveMinutes = [...container.querySelectorAll('[role="button"], button')].find((b) => b.textContent.trim() === '5m');
        await act(async () => fiveMinutes.click());
        await act(async () => {});

        const urls = globalThis.fetch.mock.calls.map(([url]) => String(url));
        expect(urls.at(-1)).toContain('window=5m');
        expect(urls.at(-1)).toContain(`vehicle=${encodeURIComponent(VEHICLE)}`);
    });

    it('does not poll while inactive', async () => {
        vi.useFakeTimers();
        respondWith(okView);

        await render({ active: false });
        await act(async () => vi.advanceTimersByTime(POLL_INTERVAL_MS * 2));

        expect(globalThis.fetch).not.toHaveBeenCalled();
    });

    it('polls again while active', async () => {
        vi.useFakeTimers();
        respondWith(okView);

        await render();
        const first = globalThis.fetch.mock.calls.length;
        await act(async () => vi.advanceTimersByTime(POLL_INTERVAL_MS));

        expect(globalThis.fetch.mock.calls.length).toBeGreaterThan(first);
    });

    it('does not start another request while one is still running', async () => {
        vi.useFakeTimers();
        globalThis.fetch = vi.fn(() => new Promise(() => {}));

        await render();
        await act(async () => vi.advanceTimersByTime(POLL_INTERVAL_MS * 3));

        expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    });

    it('does not keep showing the previous vehicle after switching', async () => {
        respondWith(okView);
        const { container, rerender } = await render();
        expect(container.textContent).toContain('encoder');

        globalThis.fetch = vi.fn(async () => { throw new TypeError('Failed to fetch'); });
        await rerender({ vehicles: [{ name: '100.101.84.41.other-drone' }] });

        expect(container.textContent).not.toContain('encoder');
        expect(container.textContent).toContain('Failed to fetch');
    });

    it('shows the network round trip', async () => {
        respondWith({ ...okView, network: { rtt_min_seconds: 0.071, rtt_mean_seconds: 0.084 } });

        const { container } = await render();

        expect(container.textContent).toContain('Network RTT');
        expect(container.textContent).toContain('min 71 ms');
        expect(container.textContent).toContain('mean 84 ms');
    });

    it('shows no network round trip without one', async () => {
        respondWith(okView);

        const { container } = await render();

        expect(container.textContent).not.toContain('Network RTT');
    });

    it('warns when frames are stamped in the future', async () => {
        respondWith({ ...okView, clock_skew_share: 0.05 });

        const { container } = await render();

        expect(container.textContent).toContain('Clock skew');
        expect(container.textContent).toContain('5%');
    });
});
