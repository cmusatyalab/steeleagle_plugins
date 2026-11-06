// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { transferPath, startTransfer, validFileName, addUpload, patchUpload, isUploading } from './fileTransfer.js';

// A WebSocket the test drives: it records what the page sends and lets the
// test play the server.
class FakeSocket {
    static last = null;
    constructor(url) {
        this.url = url;
        this.sent = [];
        this.closed = false;
        FakeSocket.last = this;
    }
    send(data) { this.sent.push(data); }
    close() {
        if (this.closed) return;
        this.closed = true;
        this.onclose?.({ code: 1000 });
    }
    serve(msg) { this.onmessage?.({ data: JSON.stringify(msg) }); }
}

const blobText = (b) => b.text();

describe('transferPath', () => {
    it('carries the daemon, kind, name and size', () => {
        const url = new URL(transferPath('100.64.0.2', 'model', 'yolo v8.pt', 12), 'http://gcs');
        expect(url.pathname).toBe('/ws/daemons/transfer');
        expect(Object.fromEntries(url.searchParams)).toEqual({ ip: '100.64.0.2', kind: 'model', name: 'yolo v8.pt', size: '12' });
    });
});

describe('startTransfer', () => {
    it('sends a chunk per acknowledgement and resolves once saved', async () => {
        const file = new Blob(['0123456789']);
        const progress = [];
        const t = startTransfer({ url: 'ws://gcs/x', file, onProgress: (s, n) => progress.push([s, n]), WebSocketImpl: FakeSocket });
        const ws = FakeSocket.last;
        ws.serve({ type: 'ready', chunk_size: 4 });
        ws.serve({ type: 'progress', sent: 4, total: 10 });
        ws.serve({ type: 'progress', sent: 8, total: 10 });
        ws.serve({ type: 'done', size: 10 });
        await expect(t.done).resolves.toEqual({ size: 10 });
        expect(await Promise.all(ws.sent.map(blobText))).toEqual(['0123', '4567', '89']);
        expect(progress).toEqual([[4, 10], [8, 10], [10, 10]]);
        expect(ws.closed).toBe(true);
    });

    it('sends one empty chunk for an empty file', async () => {
        const t = startTransfer({ url: 'ws://gcs/x', file: new Blob([]), onProgress: () => {}, WebSocketImpl: FakeSocket });
        const ws = FakeSocket.last;
        ws.serve({ type: 'ready', chunk_size: 4 });
        ws.serve({ type: 'done', size: 0 });
        await t.done;
        expect(ws.sent.map((b) => b.size)).toEqual([0]);
    });

    it("rejects with the server's error", async () => {
        const t = startTransfer({ url: 'ws://gcs/x', file: new Blob(['abc']), onProgress: () => {}, WebSocketImpl: FakeSocket });
        FakeSocket.last.serve({ type: 'error', detail: 'permission denied' });
        await expect(t.done).rejects.toThrow('permission denied');
    });

    it('rejects if the socket closes before the file is saved', async () => {
        const t = startTransfer({ url: 'ws://gcs/x', file: new Blob(['abc']), onProgress: () => {}, WebSocketImpl: FakeSocket });
        FakeSocket.last.serve({ type: 'ready', chunk_size: 4 });
        FakeSocket.last.onclose({ code: 1006 });
        await expect(t.done).rejects.toThrow('The connection to the GCS closed before the upload finished');
    });

    it('cancel closes the socket and rejects as cancelled', async () => {
        const t = startTransfer({ url: 'ws://gcs/x', file: new Blob(['abc']), onProgress: () => {}, WebSocketImpl: FakeSocket });
        FakeSocket.last.serve({ type: 'ready', chunk_size: 4 });
        t.cancel();
        expect(FakeSocket.last.closed).toBe(true);
        await expect(t.done).rejects.toMatchObject({ cancelled: true });
    });
});

describe('validFileName', () => {
    it('accepts plain names', () => {
        expect(validFileName('yolov8n.onnx')).toBeNull();
        expect(validFileName('.hidden')).toBeNull();
    });

    it('rejects empty names, directories and dot names', () => {
        for (const name of ['', '.', '..', 'a/b', 'a\\b', '../x']) {
            expect(validFileName(name)).not.toBeNull();
        }
    });
});

describe('the upload list', () => {
    const entry = { id: 1, name: 'a', kind: 'file', size: 10, sent: 0, status: 'uploading', detail: '' };

    it('adds the newest upload first, per daemon', () => {
        let s = addUpload({}, '100.64.0.2', entry);
        s = addUpload(s, '100.64.0.2', { ...entry, id: 2 });
        s = addUpload(s, '100.64.0.3', { ...entry, id: 3 });
        expect(s['100.64.0.2'].map((u) => u.id)).toEqual([2, 1]);
        expect(s['100.64.0.3'].map((u) => u.id)).toEqual([3]);
    });

    it('patches one upload', () => {
        const s = patchUpload(addUpload({}, 'd', entry), 'd', 1, { sent: 4 });
        expect(s.d[0]).toEqual({ ...entry, sent: 4 });
    });

    it('tells whether a daemon has an upload running', () => {
        const s = addUpload({}, 'd', entry);
        expect(isUploading(s, 'd')).toBe(true);
        expect(isUploading(patchUpload(s, 'd', 1, { status: 'done' }), 'd')).toBe(false);
        expect(isUploading(s, 'other')).toBe(false);
    });
});
