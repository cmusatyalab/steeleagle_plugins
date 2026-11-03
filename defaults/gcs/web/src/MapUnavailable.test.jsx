// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach } from 'vitest';
import mapboxgl from 'mapbox-gl';
import Mapbox from './Mapbox.jsx';
import MapDraw from './MapDraw.jsx';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => { document.body.innerHTML = ''; });

const render = async (el) => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    await act(async () => { createRoot(div).render(el); });
    return div;
};

// mapbox-gl's Map constructor throws without a token, which used to unmount
// the whole app to a blank page
describe('maps without a Mapbox token', () => {
    it('draws a placeholder instead of the Control/Monitor map', async () => {
        mapboxgl.accessToken = '';
        const div = await render(<Mapbox vehicles={[]} tracking={false} mapPanelSize={0} />);
        expect(div.textContent).toContain('Map unavailable');
        expect(div.textContent).toContain('Orchestrate');
    });

    it('draws a placeholder instead of the Plan map', async () => {
        mapboxgl.accessToken = '';
        const div = await render(<MapDraw features="" setFeatures={() => {}} toast={{ current: null }} />);
        expect(div.textContent).toContain('Map unavailable');
    });
});
