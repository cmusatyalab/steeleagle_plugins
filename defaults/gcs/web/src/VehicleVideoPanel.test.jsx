// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach, vi } from 'vitest';
import VehicleVideoPanel from './VehicleVideoPanel.jsx';
import { useVehicleVideo } from './useVehicleVideo.js';

vi.mock('./useVehicleVideo.js', () => ({ useVehicleVideo: vi.fn() }));
// The engine catalog isn't fetched
vi.mock('./flowCatalog.js', async (importOriginal) => ({
    ...(await importOriginal()),
    createCatalogLoader: () => () => {},
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
    document.body.innerHTML = '';
    vi.clearAllMocks();
});

function render(status) {
    useVehicleVideo.mockReturnValue({ stream: {}, status, error: null });
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => root.render(
        <VehicleVideoPanel vehicle="100.64.0.2.drone" engine={null} setEngine={() => {}} height="20rem" noVideoLabel="No video" />,
    ));
    return { container, root };
}

// decode makes the video report a decoded size of w x h, as the browser does
// when the stream starts or changes size.
function decode(container, w, h, event = 'loadedmetadata') {
    const video = container.querySelector('video');
    Object.defineProperty(video, 'videoWidth', { value: w, configurable: true });
    Object.defineProperty(video, 'videoHeight', { value: h, configurable: true });
    act(() => video.dispatchEvent(new Event(event)));
}

const resolution = (container) => container.querySelector('[aria-label="Video resolution"]');

describe('VehicleVideoPanel', () => {
    it("shows the video's decoded resolution, and follows a change in it", () => {
        const { container, root } = render('live');
        expect(resolution(container)).toBeNull();
        decode(container, 426, 240);
        expect(resolution(container).textContent).toBe('426×240');
        decode(container, 1280, 720, 'resize');
        expect(resolution(container).textContent).toBe('1280×720');
        act(() => root.unmount());
    });

    it('shows no resolution without live video', () => {
        const { container, root } = render('connecting');
        expect(container.querySelector('video')).toBeNull();
        expect(resolution(container)).toBeNull();
        act(() => root.unmount());
    });
});
