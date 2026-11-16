export const ACTIVE = new Set(['observing', 'analyzing', 'pausing', 'stopping']);

export function runName(run) {
    return run.display_name || (run.video ? `Video · ${run.video.name}` : `Live · ${run.vehicle_id}`);
}

export function videoSectionCount(duration, sectionSeconds, mode) {
    return mode === 'sliding'
        ? 1 + Math.ceil(Math.max(0, duration - sectionSeconds))
        : Math.ceil(duration / sectionSeconds);
}

export function compiledDSLReady(row) {
    return row.status === 'completed' && row.compile_status === 'compiled';
}

function mean(values) {
    const measured = values.filter(Number.isFinite);
    return measured.length ? measured.reduce((sum, value) => sum + value, 0) / measured.length : null;
}

export function summarizeRun(session, now) {
    if (!session) return { runElapsed: null, averageVision: null, averageDSL: null };
    const { sections, status, created_at: created, updated_at: updated = created } = session;
    return {
        runElapsed: Math.max(0, (ACTIVE.has(status) ? Math.max(now, updated) : updated) - created),
        averageVision: mean(sections.map(row => row.inference_elapsed)),
        averageDSL: mean(sections.filter(compiledDSLReady).map(row => row.elapsed)),
    };
}

export function orderedSections(session) {
    return (session?.sections ?? []).toSorted((a, b) => a.index - b.index);
}

export function latestSection(sections, session) {
    if (session?.source_type === 'uploaded_video' && session.settings?.section_mode === 'sliding') {
        const result = sections.findLast(row => row.inference_text || row.error);
        if (result) return result.index;
    }
    return (sections.findLast(row => row.status !== 'queued') ?? sections.at(-1))?.index;
}
