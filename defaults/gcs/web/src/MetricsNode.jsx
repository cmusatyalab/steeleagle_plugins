import { Handle, Position } from '@xyflow/react';
import { ERROR_COLOR, NODE_COLOR } from './flowView.js';
import { NODE_WIDTH, formatEstimate, formatRate, formatSeconds, formatShare } from './metricsView.js';

const PERCENT = 100;

// A producer or engine of a running flow, with the engine's numbers over the
// window: mean time per input (the estimated median and p95 on hover), wait,
// rate, skipped share, and whether it is a bottleneck.
function MetricsNode({ data }) {
    const { kind, label, host, metrics, bottleneck } = data;
    const color = bottleneck ? ERROR_COLOR : NODE_COLOR;
    return (
        <div style={{
            background: 'var(--node-bg)',
            border: `2px solid ${color}`,
            borderRadius: 8,
            padding: '8px 12px',
            width: NODE_WIDTH,
            boxSizing: 'border-box',
            userSelect: 'none',
        }}>
            {kind === 'engine' && <Handle type="target" position={Position.Left} isConnectable={false} />}
            <div style={{ fontSize: 9, color: 'var(--node-id-color)', textTransform: 'uppercase', letterSpacing: 1 }}>{host}</div>
            <div style={{ fontWeight: 'bold', fontSize: 12, color: 'var(--node-text)' }}>{label}</div>
            {kind === 'engine' && <EngineNumbers metrics={metrics} bottleneck={bottleneck} />}
            <Handle type="source" position={Position.Right} isConnectable={false} />
        </div>
    );
}

function EngineNumbers({ metrics, bottleneck }) {
    if (!metrics) {
        return <div style={{ fontSize: 11, color: 'var(--node-chip-text)', marginTop: 4 }}>no metrics</div>;
    }
    const small = { fontSize: 10, color: 'var(--node-chip-text)' };
    return (
        <>
            <div title={`mean; median ${formatEstimate(metrics.p50_seconds)} · p95 ${formatEstimate(metrics.p95_seconds)}`}
                style={{ fontSize: 18, fontWeight: 'bold', color: bottleneck ? ERROR_COLOR : 'var(--node-text)' }}>
                {formatSeconds(metrics.mean_seconds)}
            </div>
            <div style={small}>wait {formatSeconds(metrics.wait_seconds)} · {formatRate(metrics.inputs_per_second)}</div>
            <div style={small}>skipped {formatShare(metrics.skipped_share)}</div>
            {bottleneck && (
                <div style={{ ...small, color: ERROR_COLOR, fontWeight: 'bold' }}>
                    busy {Math.round((metrics.busy_fraction ?? 0) * PERCENT)}% · bottleneck
                </div>
            )}
        </>
    );
}

export default MetricsNode;
