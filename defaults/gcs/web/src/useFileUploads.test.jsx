// @vitest-environment jsdom
import React, { act, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach } from 'vitest';
import useFileUploads from './useFileUploads.js';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

class FakeSocket {
    static all = [];
    constructor(url) {
        this.url = url;
        this.sent = [];
        this.closed = false;
        FakeSocket.all.push(this);
    }
    send(data) { this.sent.push(data); }
    close() {
        if (this.closed) return;
        this.closed = true;
        this.onclose?.({ code: 1000 });
    }
    serve(msg) { this.onmessage?.({ data: JSON.stringify(msg) }); }
}

afterEach(() => {
    FakeSocket.all = [];
    document.body.innerHTML = '';
});

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

function Probe({ onValue }) {
    const value = useFileUploads(FakeSocket);
    useEffect(() => onValue(value));
    return null;
}

// Renders the hook, exposing its latest value through out.current
function mount(out) {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => root.render(<Probe onValue={(v) => { out.current = v; }} />));
    return root;
}

describe('useFileUploads', () => {
    it('tracks an upload from start to saved', async () => {
        const out = {};
        const root = mount(out);
        act(() => out.current.start('100.64.0.2', 'model', 'yolo.pt', new Blob(['0123456789'])));
        const ws = FakeSocket.all[0];
        expect(ws.url).toContain('/ws/daemons/transfer?ip=100.64.0.2&kind=model&name=yolo.pt&size=10');
        expect(out.current.uploads['100.64.0.2'][0]).toMatchObject({ name: 'yolo.pt', kind: 'model', size: 10, sent: 0, status: 'uploading' });

        act(() => {
            ws.serve({ type: 'ready', chunk_size: 4 });
            ws.serve({ type: 'progress', sent: 4, total: 10 });
        });
        expect(out.current.uploads['100.64.0.2'][0].sent).toBe(4);

        act(() => ws.serve({ type: 'done', size: 10 }));
        await flush();
        expect(out.current.uploads['100.64.0.2'][0]).toMatchObject({ sent: 10, status: 'done' });
        act(() => root.unmount());
    });

    it('marks a failed upload with its reason, and a cancelled one as cancelled', async () => {
        const out = {};
        const root = mount(out);
        act(() => out.current.start('d', 'file', 'a', new Blob(['abc'])));
        act(() => FakeSocket.all[0].serve({ type: 'error', detail: 'denied' }));
        await flush();
        expect(out.current.uploads.d[0]).toMatchObject({ status: 'failed', detail: 'denied' });

        act(() => out.current.start('d', 'file', 'b', new Blob(['abc'])));
        const id = out.current.uploads.d[0].id;
        act(() => out.current.cancel(id));
        await flush();
        expect(out.current.uploads.d[0]).toMatchObject({ name: 'b', status: 'cancelled' });
        act(() => root.unmount());
    });

    it('cancels running uploads when the page goes away', () => {
        const out = {};
        const root = mount(out);
        act(() => out.current.start('d', 'file', 'a', new Blob(['abc'])));
        act(() => root.unmount());
        expect(FakeSocket.all[0].closed).toBe(true);
    });
});
