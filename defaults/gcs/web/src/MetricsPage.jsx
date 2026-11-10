import { useEffect, useMemo, useState } from 'react';
import { ReactFlow, ReactFlowProvider, Background, Controls } from '@xyflow/react';
import { Dropdown } from 'primereact/dropdown';
import { SelectButton } from 'primereact/selectbutton';
import { Message } from 'primereact/message';
import MetricsNode from './MetricsNode.jsx';
import { flowCanvasThemeVars } from './canvasTheme.js';
import { displayName } from './vehicleNames.js';
import { getApiUrl } from './urls.js';
import { DEFAULT_WINDOW, POLL_INTERVAL_MS, WINDOWS, formatSeconds, formatShare, toReactFlow } from './metricsView.js';
import '@xyflow/react/dist/style.css';

const nodeTypes = { metrics: MetricsNode };
// Room the menu bar and the controls above the canvas take.
const CANVAS_HEIGHT = 'calc(100vh - 220px)';

// The Metrics page: a vehicle's running flow, with each engine's numbers over
// the chosen window, from Prometheus via the GCS. While active, it asks for
// fresh numbers POLL_INTERVAL_MS after each answer, so a slow answer never
// piles requests up; while hidden it asks for none.
function MetricsPage({ vehicles, theme, active }) {
    const names = useMemo(() => (vehicles ?? []).map((v) => v.name), [vehicles]);
    const [picked, setPicked] = useState(null);
    const vehicle = picked && names.includes(picked) ? picked : names[0] ?? null;
    const [timeWindow, setTimeWindow] = useState(DEFAULT_WINDOW);
    // The last answer, with the vehicle and window it is for: one for another
    // vehicle or window is never shown.
    const shown = `${vehicle}|${timeWindow}`;
    const [result, setResult] = useState({ key: null, view: null, error: null });
    const view = result.key === shown ? result.view : null;
    const error = result.key === shown ? result.error : null;

    useEffect(() => {
        if (!active || !vehicle) return undefined;
        const key = `${vehicle}|${timeWindow}`;
        const controller = new AbortController();
        let timer;
        const load = async () => {
            const query = new URLSearchParams({ vehicle, window: timeWindow });
            try {
                const response = await fetch(getApiUrl(`/api/metrics/flow?${query}`), { signal: controller.signal });
                const body = await response.json();
                setResult(response.ok
                    ? { key, view: body, error: null }
                    : { key, view: null, error: body.detail ?? `HTTP ${response.status}` });
            } catch (e) {
                if (controller.signal.aborted) return;
                setResult({ key, view: null, error: e.message ?? String(e) });
            }
            if (!controller.signal.aborted) timer = setTimeout(load, POLL_INTERVAL_MS);
        };
        load();
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [active, vehicle, timeWindow]);

    const graph = useMemo(() => (view && view.nodes.length ? toReactFlow(view) : null), [view]);

    return (
        <div className="p-3" style={flowCanvasThemeVars(theme)}>
            <div className="flex align-items-center gap-3 mb-2 flex-wrap">
                <Dropdown value={vehicle} options={names.map((n) => ({ label: displayName(n), value: n }))}
                    onChange={(e) => setPicked(e.value)} placeholder="No vehicle running" disabled={!names.length} />
                <SelectButton value={timeWindow} options={WINDOWS} allowEmpty={false}
                    onChange={(e) => e.value && setTimeWindow(e.value)} />
                <span style={{ opacity: 0.6, fontSize: 12 }}>refreshes every {POLL_INTERVAL_MS / 1000} s</span>
            </div>
            {error && <Message severity="error" text={error} className="mb-2" />}
            {view?.state === 'prometheus_unreachable' && (
                <Message severity="warn" className="mb-2"
                    text={`Prometheus unreachable, so the flow is shown without numbers: ${view.detail}`} />
            )}
            {view?.state === 'no_flow' && <Message severity="info" className="mb-2" text="This vehicle runs no flow." />}
            {view?.clock_skew_share != null && (
                <Message severity="warn" className="mb-2"
                    text={`Clock skew: ${formatShare(view.clock_skew_share)} of frames reached the GCS before their capture time by the two hosts' clocks. Capture → GCS leaves them out, so it reads low or is missing; sync the clocks (NTP).`} />
            )}
            {view?.end_to_end && (
                <div className="mb-2">
                    <div style={{ fontSize: 14 }}>
                        Capture → GCS: <b>p50 {formatSeconds(view.end_to_end.p50_seconds)} · p95 {formatSeconds(view.end_to_end.p95_seconds)}</b>
                    </div>
                    <div style={{ fontSize: 11, opacity: 0.65 }}>
                        Engine times don't add up to this: capture, transfer and queueing aren't engine time.
                    </div>
                </div>
            )}
            {view?.network && (
                <div className="mb-2">
                    <div style={{ fontSize: 14 }}>
                        Network RTT: <b>min {formatSeconds(view.network.rtt_min_seconds)} · mean {formatSeconds(view.network.rtt_mean_seconds)}</b>
                    </div>
                    <div style={{ fontSize: 11, opacity: 0.65 }}>
                        Vehicle ↔ remote server, by QUIC. Min ≈ round trip propagation delay.
                    </div>
                </div>
            )}
            {graph && (
                <div style={{ height: CANVAS_HEIGHT }}>
                    <ReactFlowProvider>
                        <ReactFlow id="flow-metrics" key={vehicle} colorMode={theme} nodes={graph.nodes} edges={graph.edges}
                            nodeTypes={nodeTypes} nodesDraggable={false} nodesConnectable={false}
                            elementsSelectable={false} fitView fitViewOptions={{ maxZoom: 1 }}>
                            <Background />
                            <Controls showInteractive={false} />
                        </ReactFlow>
                    </ReactFlowProvider>
                </div>
            )}
        </div>
    );
}

export default MetricsPage;
