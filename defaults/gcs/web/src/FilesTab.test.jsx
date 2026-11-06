// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, it, expect, afterEach, vi } from 'vitest';
import FilesTab from './FilesTab.jsx';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
    document.body.innerHTML = '';
});

function render(props) {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const all = { daemon: { ip: '100.64.0.2', state: 'up' }, uploads: [], onStart: () => {}, onCancel: () => {}, ...props };
    act(() => root.render(<FilesTab {...all} />));
    return { container, root, rerender: (p) => act(() => root.render(<FilesTab {...all} {...p} />)) };
}

const button = (container, label) => [...container.querySelectorAll('button')].find((b) => b.textContent.includes(label));

function choose(container, file) {
    const input = container.querySelector('input[type=file]');
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    act(() => input.dispatchEvent(new Event('change', { bubbles: true })));
}

function setName(container, value) {
    const input = container.querySelector('input[aria-label="Save as"]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    act(() => {
        setter.call(input, value);
        input.dispatchEvent(new Event('input', { bubbles: true }));
    });
}

describe('FilesTab', () => {
    it('uploads the chosen file under its own name, as a file by default', () => {
        const onStart = vi.fn();
        const { container, root } = render({ onStart });
        expect(button(container, 'Upload').disabled).toBe(true);
        const file = new File(['abc'], 'geofence.kml');
        choose(container, file);
        expect(container.querySelector('input[aria-label="Save as"]').value).toBe('geofence.kml');
        act(() => button(container, 'Upload').click());
        expect(onStart).toHaveBeenCalledWith('file', 'geofence.kml', file);
        act(() => root.unmount());
    });

    it('uploads as a model, under an edited name', () => {
        const onStart = vi.fn();
        const { container, root } = render({ onStart });
        const file = new File(['w'], 'best.pt');
        choose(container, file);
        act(() => button(container, 'Model').click());
        setName(container, 'yolo.pt');
        act(() => button(container, 'Upload').click());
        expect(onStart).toHaveBeenCalledWith('model', 'yolo.pt', file);
        act(() => root.unmount());
    });

    it("won't upload under a name the daemon refuses", () => {
        const { container, root } = render({});
        choose(container, new File(['a'], 'a.txt'));
        setName(container, '../a.txt');
        expect(button(container, 'Upload').disabled).toBe(true);
        expect(container.textContent).toContain("without '/'");
        act(() => root.unmount());
    });

    it('shows each upload with its bytes, and cancels a running one', () => {
        const onCancel = vi.fn();
        const uploads = [
            { id: 2, name: 'yolov8n.onnx', kind: 'model', size: 13002342, sent: 6 * 1024 * 1024, status: 'uploading', detail: '' },
            { id: 1, name: 'labels.txt', kind: 'model', size: 4198, sent: 0, status: 'failed', detail: 'permission denied' },
        ];
        const { container, root } = render({ uploads, onCancel });
        expect(container.textContent).toContain('6.0 MB / 12.4 MB · 48%');
        expect(container.textContent).toContain('permission denied');
        act(() => button(container, 'Cancel').click());
        expect(onCancel).toHaveBeenCalledWith(2);
        act(() => root.unmount());
    });

    it('allows one upload at a time', () => {
        const uploads = [{ id: 1, name: 'a', kind: 'file', size: 10, sent: 0, status: 'uploading', detail: '' }];
        const { container, root } = render({ uploads });
        choose(container, new File(['a'], 'b.txt'));
        expect(button(container, 'Upload').disabled).toBe(true);
        act(() => root.unmount());
    });
});
