// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach, vi } from 'vitest';
import PackagesTab from './PackagesTab.jsx';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = '';
});

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

describe('PackagesTab', () => {
    it("doesn't show the last daemon's packages after switching to one whose list fails", async () => {
        vi.stubGlobal('fetch', vi.fn(async (url) => (String(url).includes('ip=100.64.0.1')
            ? { status: 200, json: async () => ({ packages: [{ uri: 'github.com/a/only-on-a', plugins: [], modified: '' }] }) }
            : { status: 502, json: async () => ({ detail: 'daemon B is down' }) })));
        const toast = { current: { show: () => {} } };
        const container = document.createElement('div');
        document.body.appendChild(container);
        const root = createRoot(container);
        const tab = (ip) => <PackagesTab daemon={{ ip, state: 'up' }} toast={toast} pending={{}} onPending={() => {}} onRestartNow={() => {}} />;

        await act(async () => root.render(tab('100.64.0.1')));
        await flush();
        expect(container.textContent).toContain('github.com/a/only-on-a');

        await act(async () => root.render(tab('100.64.0.2')));
        await flush();
        expect(container.textContent).toContain('daemon B is down');
        expect(container.textContent).not.toContain('github.com/a/only-on-a');
        await act(async () => root.unmount());
    });
});
