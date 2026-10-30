// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach, vi } from 'vitest';
import ConfigureDialog from './ConfigureDialog.jsx';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = '';
});

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

const service = (name) => ({
    name, type: 'engine', auth: '', simulate: false,
    plugin: { plugin: name, package: 'github.com/org/repo', isolation: '', environment: '', args: null, extra: null },
    extra: null,
});

const config = {
    raw_toml: '', config_hash: 'h', own: false, self_service: '', form_error: null,
    form: {
        daemon: { port: 9000, lcs: null, hostname: 'swap', cert: '', orchestrator: null, extra: null },
        simulator: null,
        services: [service('decoder'), service('yolo')],
        extra: null,
    },
};

// Renders the dialog for a daemon whose config has decoder and yolo, and
// returns the open cards' service names and the notice.
async function open(focus) {
    vi.stubGlobal('fetch', vi.fn(async (url) => ({
        status: 200,
        json: async () => (String(url).includes('/packages') ? { packages: [] } : config),
    })));
    Element.prototype.scrollIntoView = vi.fn();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => root.render(
        <ConfigureDialog daemon={{ ip: '100.64.0.2', hostname: 'swap' }} focus={focus} onHide={() => {}} onApplied={() => {}} />,
    ));
    await flush();
    await flush();
    const cards = [...document.querySelectorAll('[id^="service-"]')];
    const opened = cards.filter((c) => c.textContent.includes('Pixi environment'))
        .map((c) => c.querySelector('.font-semibold').textContent);
    const result = { opened, text: document.body.textContent, scrolled: Element.prototype.scrollIntoView.mock.calls.length };
    await act(async () => root.unmount());
    return result;
}

describe('ConfigureDialog focus', () => {
    it("opens on the focused service's card", async () => {
        const { opened, scrolled } = await open('yolo');
        expect(opened).toEqual(['yolo']);
        expect(scrolled).toBe(1);
    });

    it("says so when the focused service isn't in the config", async () => {
        const { opened, text } = await open('stray');
        expect(opened).toEqual([]);
        expect(text).toContain("stray isn't in this daemon's config.");
    });

    it('opens on the Daemon tab without a focus', async () => {
        const { opened, scrolled } = await open(null);
        expect(opened).toEqual([]);
        expect(scrolled).toBe(0);
    });
});
