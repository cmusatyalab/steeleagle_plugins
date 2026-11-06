import { useCallback, useEffect, useRef, useState } from 'react';
import { addUpload, patchUpload, startTransfer, transferPath } from './fileTransfer.js';
import { getWebSocketUrl } from './urls.js';

// The Orchestrate page's file and model uploads, by daemon IP. They belong to
// the page rather than its Files tab, so they keep going while another tab,
// daemon or page is shown (the app keeps its pages mounted); unmounting the
// page, as closing the browser tab does, cancels any still running.
export default function useFileUploads(WebSocketImpl = WebSocket) {
    const [uploads, setUploads] = useState({});
    // The running transfers' cancel functions, by upload id
    const running = useRef(new Map());
    const nextId = useRef(1);

    const start = useCallback((ip, kind, name, file) => {
        const id = nextId.current++;
        const patch = (p) => setUploads((u) => patchUpload(u, ip, id, p));
        setUploads((u) => addUpload(u, ip, { id, name, kind, size: file.size, sent: 0, status: 'uploading', detail: '' }));
        const t = startTransfer({
            url: getWebSocketUrl(transferPath(ip, kind, name, file.size)),
            file,
            onProgress: (sent) => patch({ sent }),
            WebSocketImpl,
        });
        running.current.set(id, t.cancel);
        t.done.then(
            () => patch({ status: 'done' }),
            (err) => patch(err.cancelled ? { status: 'cancelled' } : { status: 'failed', detail: err.message }),
        ).finally(() => running.current.delete(id));
    }, [WebSocketImpl]);

    const cancel = useCallback((id) => running.current.get(id)?.(), []);

    useEffect(() => {
        const transfers = running.current;
        return () => transfers.forEach((c) => c());
    }, []);

    return { uploads, start, cancel };
}
