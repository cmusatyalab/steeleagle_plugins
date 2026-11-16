// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach, vi } from 'vitest';
import PluginsTab from './PluginsTab.jsx';
import { restartService, stopService } from './daemonsApi.js';

vi.mock('./daemonsApi.js', () => ({
    restartService: vi.fn(async () => null),
    stopService: vi.fn(async () => null),
}));
// Every confirmation is accepted at once
vi.mock('primereact/confirmdialog', () => ({ confirmDialog: ({ accept }) => accept() }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
    document.body.innerHTML = '';
    vi.clearAllMocks();
});

const service = (bare, props) => ({
    name: `100.64.0.2.${bare}`, bare, plugin: bare, category: 'engine', running: true, stopped: false, auth: 'admin', self: false, ...props,
});

const services = [
    service('yolo'),
    service('detector', { running: false, stopped: true }),
    service('rescale', { running: false }),
    service('gcs', { self: true }),
];

function render(props) {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const all = {
        daemon: { ip: '100.64.0.2', state: 'up', services },
        toast: { current: { show: vi.fn() } },
        onRestarted: vi.fn(),
        onConfigure: () => {},
        configureDisabled: false,
        ...props,
    };
    act(() => root.render(<PluginsTab {...all} />));
    return { container, root, ...all };
}

const button = (container, label) => container.querySelector(`button[aria-label="${label}"]`);
const row = (container, bare) => [...container.querySelectorAll('tbody tr')].find((r) => r.textContent.includes(bare));

describe('PluginsTab', () => {
    it('tells a service stopped by request from one that exited', () => {
        const { container, root } = render();
        expect(row(container, 'yolo').textContent).toContain('Running');
        expect(row(container, 'detector').textContent).toContain('Stopped by request');
        expect(row(container, 'rescale').textContent).toContain('Stopped');
        expect(row(container, 'rescale').textContent).not.toContain('by request');
        act(() => root.unmount());
    });

    it('stops a service, then refreshes the list', async () => {
        const { container, root, onRestarted, toast } = render();
        await act(async () => button(container, 'Stop yolo').click());
        expect(stopService).toHaveBeenCalledWith('100.64.0.2.yolo');
        expect(onRestarted).toHaveBeenCalled();
        expect(toast.current.show).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success', summary: 'Stop yolo' }));
        act(() => root.unmount());
    });

    it('starts a service stopped by request, which has no stop or restart', async () => {
        const { container, root, onRestarted } = render();
        expect(button(container, 'Stop detector')).toBeNull();
        expect(button(container, 'Restart detector')).toBeNull();
        await act(async () => button(container, 'Start detector').click());
        expect(restartService).toHaveBeenCalledWith('100.64.0.2.detector');
        expect(onRestarted).toHaveBeenCalled();
        act(() => root.unmount());
    });

    it("doesn't offer to stop the GCS itself", () => {
        const { container, root } = render();
        expect(button(container, 'Stop gcs')).toBeNull();
        expect(button(container, 'Restart gcs')).not.toBeNull();
        act(() => root.unmount());
    });

    it('shows why a stop failed', async () => {
        stopService.mockResolvedValueOnce('no service "x"');
        const { container, root, onRestarted, toast } = render();
        await act(async () => button(container, 'Stop yolo').click());
        expect(toast.current.show).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', detail: 'no service "x"' }));
        expect(onRestarted).not.toHaveBeenCalled();
        act(() => root.unmount());
    });
});
