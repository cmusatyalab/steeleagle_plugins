// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { fetchPins, putPinSettings, clearPins } from './detectionPins.js';
import DetectionSettings from './DetectionSettings.jsx';

vi.mock('./detectionPins.js', () => ({ fetchPins: vi.fn(), putPinSettings: vi.fn(), clearPins: vi.fn() }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

let root;
let container;

beforeEach(() => {
    fetchPins.mockResolvedValue({ settings: { radius_m: 7, expiry_s: 300, min_sightings: 2 }, pins: [{ id: '1' }, { id: '2' }] });
    putPinSettings.mockImplementation(async (s) => s);
    clearPins.mockResolvedValue({ cleared: 2 });
});

afterEach(() => {
    act(() => root.unmount());
    vi.clearAllMocks();
    document.body.innerHTML = '';
});

async function render() {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root.render(<DetectionSettings />));
    await flush();
}

const button = (text) => [...container.querySelectorAll('button')].find((b) => b.textContent.includes(text));

describe('DetectionSettings', () => {
    it('shows the shared settings from the server', async () => {
        await render();
        const text = container.textContent;
        expect(text).toContain('Shared by everyone viewing this GCS');
        expect(text).toContain('7m');
        expect(text).toContain('5min');
        expect(text).toContain('Grouping Radius');
        expect(text).toContain('Pin Expiry');
        expect(text).toContain('Min Sightings');
    });

    it('asks before clearing, then clears for everyone', async () => {
        await render();
        await act(async () => button('Clear all pins').click());
        expect(container.textContent).toContain('Clear all 2 pins for everyone?');
        expect(clearPins).not.toHaveBeenCalled();
        await act(async () => button('Cancel').click());
        expect(container.textContent).not.toContain('for everyone?');
        await act(async () => button('Clear all pins').click());
        await act(async () => button('Clear').click());
        await flush();
        expect(clearPins).toHaveBeenCalledTimes(1);
    });
});
