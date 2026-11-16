import { describe, expect, it } from 'vitest';
import { latestSection, orderedSections, summarizeRun, videoSectionCount } from './runReport.js';

describe('video section count', () => {
    it('includes the clamped final window and treats a short clip as one section', () => {
        expect(videoSectionCount(6.25, 5, 'sliding')).toBe(3);
        expect(videoSectionCount(6, 5, 'sliding')).toBe(2);
        expect(videoSectionCount(2, 5, 'sliding')).toBe(1);
        expect(videoSectionCount(6.25, 5, 'non_overlapping')).toBe(2);
    });
});

describe('run report', () => {
    it('follows the highest-index visual result or error despite out-of-order updates', () => {
        const session = {
            source_type: 'uploaded_video', settings: { section_mode: 'sliding' },
            sections: [
                { index: 4, status: 'inferring' },
                { index: 3, status: 'failed', error: 'Missing footage' },
                { index: 1, status: 'completed', inference_text: 'Wait' },
                { index: 2, status: 'generating', inference_text: 'Patrol' },
            ],
        };
        const sections = orderedSections(session);
        expect(sections.map(row => row.index)).toEqual([1, 2, 3, 4]);
        expect(latestSection(sections, session)).toBe(3);
        expect(latestSection(sections, { ...session, settings: { section_mode: 'non_overlapping' } })).toBe(4);
    });

    it('freezes paused wall time and omits missing and uncompiled DSL timings from means', () => {
        const session = {
            status: 'paused', created_at: 100, updated_at: 115, sections: [
                { status: 'completed', compile_status: 'compiled', inference_elapsed: 4, elapsed: 10 },
                { status: 'completed', compile_status: 'compiled', elapsed: 8 },
                { status: 'completed', compile_status: 'needs_context', inference_elapsed: 3, elapsed: 20 },
                { status: 'generating', inference_elapsed: 2 },
                { status: 'failed' },
            ],
        };
        const paused = summarizeRun(session, 200);
        expect(paused).toEqual({ runElapsed: 15, averageVision: 3, averageDSL: 9 });
        expect(summarizeRun({ ...session, status: 'analyzing' }, 200).runElapsed).toBe(100);
        expect(summarizeRun({ ...session, sections: [] }, 200)).toEqual({
            runElapsed: 15, averageVision: null, averageDSL: null,
        });
    });
});
