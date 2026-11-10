import { describe, it, expect } from 'vitest';
import { NODE_HEIGHT, NODE_WIDTH, formatRate, formatSeconds, formatShare, layout, toReactFlow } from './metricsView.js';

const diamond = {
    nodes: [
        { id: 'camera', kind: 'producer' },
        { id: 'a', kind: 'engine' },
        { id: 'b', kind: 'engine' },
        { id: 'c', kind: 'engine' },
    ],
    edges: [
        { from: 'camera', to: 'a' },
        { from: 'camera', to: 'b' },
        { from: 'a', to: 'c' },
        { from: 'b', to: 'c' },
    ],
};

describe('layout', () => {
    it('puts a join after both of its inputs and overlaps nothing', () => {
        const at = layout(diamond.nodes, diamond.edges);

        expect(at.c.x).toBeGreaterThan(at.a.x);
        expect(at.c.x).toBeGreaterThan(at.b.x);
        expect(at.a.x).toBeGreaterThan(at.camera.x);
        const ids = Object.keys(at);
        for (const p of ids) {
            for (const q of ids) {
                if (p === q) continue;
                const apart = Math.abs(at[p].x - at[q].x) >= NODE_WIDTH || Math.abs(at[p].y - at[q].y) >= NODE_HEIGHT;
                expect(apart, `${p} overlaps ${q}`).toBe(true);
            }
        }
    });
});

describe('formatting', () => {
    it('formats seconds as ms or s', () => {
        expect(formatSeconds(0.082)).toBe('82 ms');
        expect(formatSeconds(0.0002)).toBe('0.2 ms');
        expect(formatSeconds(1.234)).toBe('1.2 s');
        expect(formatSeconds(null)).toBe('–');
    });

    it('formats shares and rates', () => {
        expect(formatShare(0.59)).toBe('59%');
        expect(formatShare(0)).toBe('0%');
        expect(formatShare(null)).toBe('–');
        expect(formatRate(12.24)).toBe('12.2/s');
        expect(formatRate(null)).toBe('–');
    });
});

describe('toReactFlow', () => {
    it('carries the bottleneck and the host into each node', () => {
        const view = {
            nodes: [
                { id: 'rgb', kind: 'producer', label: 'rgb', host: 'fermi', metrics: null, bottleneck: false },
                { id: 'enc', kind: 'engine', label: 'encoder', host: 'fermi', metrics: { p50_seconds: 0.082 }, bottleneck: true },
            ],
            edges: [{ from: 'rgb', to: 'enc' }],
        };

        const { nodes, edges } = toReactFlow(view);

        expect(nodes.map((n) => n.type)).toEqual(['metrics', 'metrics']);
        const enc = nodes.find((n) => n.id === 'enc');
        expect(enc.data).toMatchObject({ label: 'encoder', host: 'fermi', bottleneck: true });
        expect(enc.position).toEqual(expect.objectContaining({ x: expect.any(Number), y: expect.any(Number) }));
        expect(edges).toEqual([expect.objectContaining({ source: 'rgb', target: 'enc' })]);
    });
});
