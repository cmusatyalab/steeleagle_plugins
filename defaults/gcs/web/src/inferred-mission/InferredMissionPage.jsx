import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button } from 'primereact/button';
import { Dialog } from 'primereact/dialog';
import { InputText } from 'primereact/inputtext';
import { Menu } from 'primereact/menu';
import { TabPanel, TabView } from 'primereact/tabview';
import { getApiUrl } from '../urls.js';
import {
    ACTIVE, compiledDSLReady, latestSection, orderedSections, runName, summarizeRun, videoSectionCount,
} from './runReport.js';
import './inferredMission.css';

const API = '/api/inferred-mission';

function label(value) {
    return (value || 'pending').replaceAll('_', ' ');
}

function timestamp(seconds) {
    const absolute = Math.round(Math.abs(seconds || 0) * 100) / 100;
    const minutes = String(Math.floor(absolute / 60)).padStart(2, '0');
    const remainder = absolute % 60;
    const duration = Number.isInteger(remainder)
        ? String(remainder).padStart(2, '0') : remainder.toFixed(2).padStart(5, '0');
    return `${seconds < 0 ? '−' : ''}${minutes}:${duration}`;
}

function durationLabel(seconds) {
    if (!Number.isFinite(seconds)) return '—';
    return seconds >= 60 ? timestamp(seconds) : `${seconds.toFixed(2)} s`;
}

function TimingMetrics({ label, metrics }) {
    return (
        <dl className="im-timing" aria-label={label}>
            {metrics.map(([name, seconds, description]) => (
                <div key={name} title={description}>
                    <dt>{name}</dt>
                    <dd>{durationLabel(seconds)}</dd>
                </div>
            ))}
        </dl>
    );
}

function latestSnapshot(previous, next) {
    return previous?.id === next.id && previous.revision > next.revision ? previous : next;
}

async function jsonRequest(path, options) {
    const response = await fetch(getApiUrl(path), options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(typeof body.detail === 'string' ? body.detail : `Request failed (${response.status}).`);
    }
    return body;
}

function uploadVideo(file, onProgress) {
    return new Promise((resolve, reject) => {
        const form = new FormData();
        form.append('file', file);
        const xhr = new XMLHttpRequest();
        xhr.open('POST', getApiUrl(`${API}/videos`));
        xhr.upload.onprogress = event => {
            if (event.lengthComputable) onProgress(Math.round(100 * event.loaded / event.total));
        };
        xhr.onerror = () => reject(new Error('The video upload was interrupted.'));
        xhr.onload = () => {
            try {
                const body = JSON.parse(xhr.responseText);
                if (xhr.status >= 200 && xhr.status < 300) resolve(body);
                else reject(new Error(body.detail || 'The video could not be uploaded.'));
            } catch {
                reject(new Error(`The upload returned an unexpected response (${xhr.status}).`));
            }
        };
        xhr.send(form);
    });
}

function ModelStages({ label, models, promptVersion }) {
    return (
        <div className="im-models" role="group" aria-label={label}>
            <span>Vision analysis <strong>{models.vision_model || 'Unknown'}</strong>
                {promptVersion && ` · prompt ${promptVersion.toUpperCase()}`}
                {models.vision_temperature != null && models.vision_temperature !== '' && ` · temperature ${models.vision_temperature}`}
            </span>
            <span>DSL generation <strong>{models.dsl_model || 'Unknown'}</strong>
                {models.dsl_temperature != null && models.dsl_temperature !== '' && ` · temperature ${models.dsl_temperature}`}
            </span>
        </div>
    );
}

function SectionDetail({ row, session, onApply }) {
    const [frameIndex, setFrameIndex] = useState(0);
    const [applying, setApplying] = useState(false);
    const [applyError, setApplyError] = useState('');
    const videoRef = useRef(null);
    const downloadMenu = useRef(null);
    const mediaUrl = session.video ? getApiUrl(`${API}/videos/${session.video.id}/media`) : '';
    const path = getApiUrl(`${API}/sessions/${session.id}/sections/${row.index}`);
    const shownFrame = Math.min(frameIndex, Math.max(0, (row.frame_count || 1) - 1));
    const frameTime = row.frame_times?.[shownFrame];
    const frameLabel = `Frame ${shownFrame + 1} of ${row.frame_count}`
        + (Number.isFinite(frameTime) ? ` · +${frameTime.toFixed(2)} s` : '');
    const offset = session.video_takeoff_offset || 0;
    const hasVision = Boolean(row.vision_inference || row.vision_evidence);
    const separateProposal = row.vision_inference && row.vision_inference !== row.inference_text;
    const visionEvidence = row.vision_evidence && <p className="im-note"><strong>Why</strong> {row.vision_evidence}</p>;
    const missionHeading = row.compile_status === 'compiled' ? 'Compiled mission' : 'Mission candidate';
    const downloads = [
        { label: 'Download DSL', url: `${path}/files/mission.dsl`, visible: Boolean(row.dsl) },
        { label: 'Download V4 graph JSON', url: `${path}/files/mission.json`, visible: Boolean(row.mission_json) },
        { label: 'Download record', url: `${path}/files/inference.json` },
    ];

    useEffect(() => {
        const video = videoRef.current;
        if (video && video.readyState > 0) video.currentTime = row.start;
    }, [row.start, mediaUrl]);

    async function apply() {
        setApplying(true);
        setApplyError('');
        try {
            await onApply('fsm-builder', { payload: { normalized_dsl: row.dsl } });
        } catch (error) {
            setApplyError(error.message);
        } finally {
            setApplying(false);
        }
    }

    return (
        <article className="im-detail">
            <div className="im-detail-title">
                <h3>Section {String(row.index).padStart(3, '0')}</h3>
                <span>{timestamp(row.start - offset)}–{timestamp(row.end - offset)} · {label(row.input_quality)}</span>
            </div>
            <div className="im-inference">
                {separateProposal && <div className="im-vision-summary">
                    <h4>{session.source_type === 'uploaded_video' ? 'Video inference' : 'Live inference'}</h4>
                    <p>{row.vision_inference}</p>
                    {visionEvidence}
                </div>}
                {hasVision && <h4>{missionHeading}{row.inference_status === 'fallback' && ' · fallback'}</h4>}
                <p className="im-mission">{row.inference_text || `Inference ${label(row.inference_status)}.`}</p>
                {!separateProposal && visionEvidence}
                {row.adaptation && <p className="im-note">{row.adaptation}</p>}
                {row.error && <p role="alert" className="im-error">{row.error}</p>}
                {row.unresolved?.map(message => <p key={message} className="im-note">{message}</p>)}
            </div>
            <TimingMetrics label="Section timing" metrics={[
                ['Vision ready', row.inference_elapsed, 'From section processing start to a validated visual inference'],
                ['DSL ready', compiledDSLReady(row) && row.dsl ? row.elapsed : null, 'From section processing start to compiled DSL'],
                ['Frame prep', row.frame_preparation_elapsed],
                ['History wait', row.history_wait_elapsed],
                ['Vision request', row.vision_request_elapsed, 'Time spent preparing and making the vision request'],
                ['DSL generation', row.generation_elapsed, 'Includes preparing the DSL reference and compiling the result'],
            ]} />
            <div className="im-detail-grid">
                <div className="im-evidence">
                    {mediaUrl && <video ref={videoRef} className="im-media" src={mediaUrl} controls preload="metadata"
                        onLoadedMetadata={event => { event.currentTarget.currentTime = row.start; }}
                        onTimeUpdate={event => {
                            if (event.currentTarget.currentTime >= row.end) event.currentTarget.pause();
                        }} />}
                    {row.frame_count ? (
                        <details className="im-sampled-frames" open={!mediaUrl}>
                            <summary>Sampled frames <span className="im-muted">{row.frame_count} frames</span></summary>
                            <img className="im-media" src={`${path}/frames/${shownFrame}`} alt={`Sampled frame ${shownFrame + 1}`} />
                            <label className="im-frame-control">
                                <input aria-label="Sampled frame" aria-valuetext={frameLabel} type="range"
                                    min="0" max={row.frame_count - 1} value={shownFrame}
                                    onChange={event => setFrameIndex(Number(event.target.value))} />
                                <span>{frameLabel}</span>
                            </label>
                        </details>
                    ) : !mediaUrl && <p className="im-muted">No sampled frames for this section.</p>}
                </div>
                <div className="im-artifacts">
                    <div className="im-artifact-toolbar">
                        <span className="im-badge">{label(row.compile_status)}</span>
                        <Button type="button" label="Download" icon="pi pi-download" size="small"
                            outlined severity="secondary" aria-haspopup="menu"
                            onClick={event => downloadMenu.current.toggle(event)} />
                        <Menu ref={downloadMenu} model={downloads} popup />
                    </div>
                    <TabView className="im-artifact-tabs">
                        <TabPanel header="SteelEagle DSL">
                            {row.dsl ? <pre><code>{row.dsl}</code></pre> : <p className="im-muted">
                                {row.status === 'failed' || row.status === 'skipped' || row.status === 'gap' || row.compile_status === 'unsupported_task'
                                    ? 'No validated DSL candidate for this section.' : 'Waiting for a validated DSL candidate.'}
                            </p>}
                        </TabPanel>
                        <TabPanel header="V4 Graph" disabled={!row.mission_json}>
                            {row.mission_json && <>
                                <ul className="im-transitions">
                                    {row.mission_json.graph?.edges?.map(edge =>
                                        <li key={`${edge.source}/${edge.event_id}/${edge.target}`}>
                                            {edge.source} → {edge.target} <span>({edge.event_id})</span>
                                        </li>)}
                                </ul>
                                <pre><code>{JSON.stringify(row.mission_json, null, 2)}</code></pre>
                            </>}
                        </TabPanel>
                        <TabPanel header="History">
                            {session.models && <ModelStages label="Selected run models" models={session.models} promptVersion={session.prompt_version} />}
                            {row.history?.length ? <ol>
                                {row.history.map(item => <li key={item.section}>Section {item.section} · weight {item.weight.toFixed(3)} · {item.text}</li>)}
                            </ol> : <p>No prior inferred missions used.</p>}
                            {(row.attempts || []).map((attempt, index) => (
                                <details key={index} className="im-fold">
                                    <summary>Attempt {index + 1} · {label(attempt.status)}</summary>
                                    {attempt.errors.map(error => <p key={error} className="im-error">{error}</p>)}
                                    <pre><code>{attempt.normalized_dsl}</code></pre>
                                </details>
                            ))}
                            {row.telemetry && <pre>{JSON.stringify(row.telemetry, null, 2)}</pre>}
                            {row.evidence && <details><summary>Matched frame evidence</summary><pre>{JSON.stringify(row.evidence, null, 2)}</pre></details>}
                            {row.vision_reply && <details><summary>Vision model reply</summary><pre>{row.vision_reply}</pre></details>}
                            {row.vision_prompt && <details><summary>Vision prompt</summary><pre>{row.vision_prompt}</pre></details>}
                            {row.dsl_reference && <details><summary>DSL reference</summary><pre>{JSON.stringify(row.dsl_reference, null, 2)}</pre></details>}
                        </TabPanel>
                    </TabView>
                    {row.dsl && <div className="im-artifact-actions">
                        <Button type="button" label="Apply to Mission Builder" icon="pi pi-arrow-right"
                            size="small" disabled={applying} onClick={apply} />
                        {applyError && <p role="alert" className="im-error">{applyError}</p>}
                    </div>}
                </div>
            </div>
        </article>
    );
}

export default function InferredMissionPage({ activeTab, onApplyArtifact, getPlanningContext }) {
    const [config, setConfig] = useState(null);
    const [runs, setRuns] = useState([]);
    const [showArchived, setShowArchived] = useState(false);
    const [renameValue, setRenameValue] = useState(null);
    const [newRunOpen, setNewRunOpen] = useState(null);
    const newRunPanelId = useId();
    const videoInput = useRef(null);
    const [source, setSource] = useState('live');
    const [streams, setStreams] = useState([]);
    const [producer, setProducer] = useState('');
    const [takeoff, setTakeoff] = useState('');
    const [videoTakeoff, setVideoTakeoff] = useState('');
    const [historyMode, setHistoryMode] = useState('');
    const [sectionMode, setSectionMode] = useState('');
    const [asset, setAsset] = useState(null);
    const [progress, setProgress] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [sessionId, setSessionId] = useState('');
    const [snapshot, setSnapshot] = useState(null);
    const [selectedIndex, setSelectedIndex] = useState(null);
    const [detail, setDetail] = useState(null);
    const [now, setNow] = useState(() => Date.now() / 1000);
    const session = snapshot?.id === sessionId ? snapshot : null;
    const visibleRuns = runs.filter(run => Boolean(run.archived_at) === showArchived);
    const showNewRun = !showArchived && (newRunOpen ?? visibleRuns.length === 0);
    const active = Boolean(session && ACTIVE.has(session.status));
    const sections = useMemo(() => orderedSections(session), [session]);
    const queuedCount = sections.filter(row => row.status === 'queued').length;
    const { runElapsed, averageVision, averageDSL } = summarizeRun(session, now);
    const index = selectedIndex ?? latestSection(sections, session);
    const summary = sections.find(row => row.index === index);
    const detailStatus = summary?.status;
    const renameLength = [...(renameValue ?? '').trim()].length;
    const canRename = renameLength > 0 && renameLength <= 120;

    useEffect(() => {
        const controller = new AbortController();
        const options = { signal: controller.signal };
        Promise.all([jsonRequest(`${API}/status`, options), jsonRequest(`${API}/sessions?include_archived=true`, options)])
            .then(([settings, saved]) => {
                if (controller.signal.aborted) return;
                setConfig(settings);
                setRuns(saved);
                setSessionId(saved.find(run => !run.archived_at)?.id || '');
            }).catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
        return () => controller.abort();
    }, []);

    useEffect(() => {
        if (!activeTab) return undefined;
        const controller = new AbortController();
        jsonRequest(`${API}/status`, { signal: controller.signal })
            .then(settings => { if (!controller.signal.aborted) setConfig(settings); })
            .catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
        let timer;
        async function poll() {
            try {
                const rows = await jsonRequest('/api/streams', { signal: controller.signal });
                if (!controller.signal.aborted) setStreams(rows.filter(row => row.kind === 'video' &&
                    Date.now() - Date.parse(row.last_seen) < 30_000));
            } catch {
                if (!controller.signal.aborted) setStreams([]);
            }
            if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
        }
        poll();
        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [activeTab]);

    useEffect(() => {
        if (!sessionId) return undefined;
        const controller = new AbortController();
        jsonRequest(`${API}/sessions/${sessionId}`, { signal: controller.signal })
            .then(next => { if (!controller.signal.aborted) setSnapshot(previous => latestSnapshot(previous, next)); })
            .catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
        return () => controller.abort();
    }, [sessionId]);

    useEffect(() => {
        if (!sessionId || !active) return undefined;
        const events = new EventSource(getApiUrl(`${API}/sessions/${sessionId}/events`));
        const update = event => {
            const next = JSON.parse(event.data);
            setSnapshot(previous => latestSnapshot(previous, next));
        };
        events.addEventListener('update', update);
        return () => {
            events.removeEventListener('update', update);
            events.close();
        };
    }, [sessionId, active]);

    useEffect(() => {
        if (!sessionId || index == null || !detailStatus) return undefined;
        const controller = new AbortController();
        jsonRequest(`${API}/sessions/${sessionId}/sections/${index}`, { signal: controller.signal })
            .then(row => { if (!controller.signal.aborted) setDetail({ sessionId, row }); })
            .catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
        return () => controller.abort();
    }, [sessionId, index, detailStatus]);

    useEffect(() => {
        if (!active) return undefined;
        const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
        return () => clearInterval(timer);
    }, [active]);

    async function chooseFile(file) {
        if (!file || busy) return;
        setBusy(true);
        setError('');
        setAsset(null);
        setVideoTakeoff('');
        setProgress(0);
        try {
            setAsset(await uploadVideo(file, setProgress));
        } catch (failure) {
            setError(failure.message);
        } finally {
            setProgress(null);
            setBusy(false);
        }
    }

    async function start() {
        setBusy(true);
        setError('');
        try {
            const body = { source_type: source, planning_context: getPlanningContext?.() };
            if (historyMode) body.history_mode = historyMode;
            if (sectionMode) body.section_mode = sectionMode;
            if (source === 'uploaded_video') {
                body.video_asset_id = asset.id;
                if (videoTakeoff !== '') body.video_takeoff_offset = Number(videoTakeoff);
            }
            else {
                if (!selectedCamera) throw new Error('Select a streaming camera.');
                body.vehicle_id = selectedCamera.vehicle;
                body.producer = selectedCamera.producer;
                if (takeoff) body.flight_started_at = new Date(takeoff).getTime() / 1000;
            }
            const run = await jsonRequest(`${API}/sessions`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            setRuns(previous => [run, ...previous]);
            setSnapshot(run);
            setSessionId(run.id);
            setSelectedIndex(null);
            setNewRunOpen(false);
        } catch (failure) {
            setError(failure.message);
        } finally {
            setBusy(false);
        }
    }

    async function action(name) {
        setBusy(true);
        setError('');
        try {
            const next = await jsonRequest(`${API}/sessions/${sessionId}/${name}`, { method: 'POST' });
            setSnapshot(previous => latestSnapshot(previous, next));
        } catch (failure) {
            setError(failure.message);
        } finally {
            setBusy(false);
        }
    }

    function switchArchiveView() {
        const next = !showArchived;
        setShowArchived(next);
        setSessionId(runs.find(run => Boolean(run.archived_at) === next)?.id || '');
        setSelectedIndex(null);
        setNewRunOpen(null);
        setError('');
    }

    async function updateRun(changes) {
        setBusy(true);
        setError('');
        try {
            const updated = await jsonRequest(`${API}/sessions/${sessionId}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes),
            });
            setRuns(previous => previous.map(run => run.id === updated.id ? updated : run));
            setSnapshot(previous => previous?.id === updated.id && !(previous.revision > updated.revision)
                ? { ...previous, ...updated }
                : previous);
            setRenameValue(null);
            if (changes.archived !== undefined) {
                setSessionId(visibleRuns.find(run => run.id !== updated.id)?.id || '');
                setSelectedIndex(null);
                setNewRunOpen(false);
            }
        } catch (failure) {
            setError(failure.message);
        } finally {
            setBusy(false);
        }
    }

    const row = detail?.sessionId === sessionId && detail?.row.index === index ? detail.row : null;
    const frameAge = session?.last_frame_received ? Math.max(0, Math.floor(now - session.last_frame_received)) : null;
    const videoMode = source === 'uploaded_video';
    const sectionSeconds = config?.settings.section_seconds ?? 15;
    const defaultSectionMode = config?.settings.section_mode ?? 'non_overlapping';
    const videoSections = asset && videoSectionCount(asset.duration, sectionSeconds, sectionMode || defaultSectionMode);
    const selectedCamera = streams.find(stream => stream.producer === producer);
    const sourceReady = config?.media_available && (videoMode ? asset : selectedCamera);
    const canStart = Boolean(!busy && config?.configured && sourceReady);
    const liveTarget = selectedCamera ? `Camera: ${selectedCamera.producer}`
        : 'Select a streaming camera to observe.';
    const videoTarget = asset?.name || (progress == null ? 'Choose an MP4 to analyze.' : 'Preparing video…');
    const startTarget = videoMode ? videoTarget : liveTarget;
    const startTargetId = `${newRunPanelId}-start-target`;
    return (
        <section className="im-page" aria-label="Inferred Mission">
            <div className="im-run-bar">
                <div className="im-actions im-run-selection">
                    <label>{showArchived ? 'Archived run' : 'Run'} <select value={sessionId} disabled={busy} onChange={event => {
                        const runId = event.target.value;
                        setSessionId(runId);
                        setSelectedIndex(null);
                        setNewRunOpen(!runId);
                    }}>
                        <option value="">{showArchived && !visibleRuns.length ? 'No archived runs' : 'Select an analysis run'}</option>
                        {visibleRuns.map(run => <option value={run.id} key={run.id}>
                            {runName(run)} · {new Date(run.created_at * 1000).toLocaleString()}
                        </option>)}
                    </select></label>
                    {session && <>
                        <span className="im-badge">{label(session.status)}</span>
                        <span className="im-muted">
                            {sections.length}{session.total_sections ? ` / ${session.total_sections}` : ''} sections
                            {queuedCount > 0 && ` · ${queuedCount} queued`}
                        </span>
                    </>}
                </div>
                <div className="im-actions">
                    {session && <button type="button" disabled={busy} onClick={() => {
                        setError('');
                        setRenameValue(runName(session));
                    }}>Rename</button>}
                    {session && (showArchived
                        ? <button type="button" disabled={busy} onClick={() => updateRun({ archived: false })}>Restore</button>
                        : <button type="button" disabled={busy || active} onClick={() => updateRun({ archived: true })}>Archive</button>)}
                    <button type="button" disabled={busy} onClick={switchArchiveView}>{showArchived ? 'Analysis runs' : 'Archived runs'}</button>
                    {!showArchived && session?.status === 'analyzing' && <button type="button" disabled={busy} onClick={() => action('pause')}>Pause</button>}
                    {!showArchived && session?.status === 'paused' && <button type="button" disabled={busy} onClick={() => action('resume')}>Resume</button>}
                    {!showArchived && (active || session?.status === 'paused') && <button type="button" disabled={busy || session.status === 'stopping'} onClick={() => action('stop')}>Stop</button>}
                    <Button type="button" label="New run" icon={showNewRun ? 'pi pi-chevron-up' : 'pi pi-chevron-down'}
                        size="small" outlined disabled={busy || showArchived} aria-expanded={showNewRun} aria-controls={newRunPanelId}
                        onClick={() => setNewRunOpen(!showNewRun)} />
                </div>
            </div>
            <Dialog header="Rename analysis run" visible={renameValue !== null} modal closable={!busy} closeOnEscape={!busy}
                onHide={() => { if (!busy) setRenameValue(null); }}>
                <form className="im-actions im-rename" onSubmit={event => {
                    event.preventDefault();
                    if (!busy && canRename) updateRun({ display_name: renameValue.trim() });
                }}>
                    <label>Display name <InputText autoFocus value={renameValue ?? ''} required disabled={busy}
                        aria-invalid={renameLength > 120}
                        onChange={event => setRenameValue(event.target.value)} /></label>
                    <Button type="submit" label="Save" size="small" disabled={busy || !canRename} />
                    {renameLength > 120 && <small role="alert">Use at most 120 characters.</small>}
                    {error && <small role="alert">{error}</small>}
                </form>
            </Dialog>
            <div className="im-input" id={newRunPanelId} hidden={!showNewRun}>
                <div className="im-run-settings">
                    <div className="im-source" aria-label="Footage source">
                        <button type="button" aria-pressed={!videoMode} onClick={() => setSource('live')}>Live observation</button>
                        <button type="button" aria-pressed={videoMode} onClick={() => setSource('uploaded_video')}>Upload MP4</button>
                    </div>
                    {videoMode ? (
                        <div className={asset ? 'im-upload im-upload-ready' : 'im-upload'} onDragOver={event => event.preventDefault()}
                            onDrop={event => { event.preventDefault(); chooseFile(event.dataTransfer.files[0]); }}>
                            <input ref={videoInput} type="file" accept=".mp4,video/mp4" disabled={busy} hidden
                                onChange={event => { chooseFile(event.target.files[0]); event.target.value = ''; }} />
                            <div className="im-upload-row">
                                <div className="im-upload-info">
                                    {asset ? <>
                                        <strong>{asset.name}</strong>
                                        <p className="im-muted">
                                            {asset.duration.toFixed(1)} s · {videoSections} sections
                                        </p>
                                    </> : progress != null ? (
                                        <p role="status">{progress < 100 ? `Uploading ${progress}%` : 'Checking video…'}</p>
                                    ) : <p className="im-muted">Drop an MP4 here</p>}
                                </div>
                                <Button type="button" label={asset ? 'Change file' : 'Choose MP4'} icon="pi pi-folder-open"
                                    size="small" outlined severity="secondary" disabled={busy}
                                    onClick={() => videoInput.current?.click()} />
                            </div>
                            {config && !config.media_available && <p className="im-note">Install FFmpeg and ffprobe on the GCS backend.</p>}
                        </div>
                    ) : (
                        <div className="im-live-fields">
                            <label className="im-vehicle-field">Camera <select value={selectedCamera?.producer ?? ''} onChange={event => setProducer(event.target.value)}>
                                <option value="">{streams.length ? 'Select a camera' : 'No cameras streaming'}</option>
                                {streams.map(stream => <option key={stream.producer} value={stream.producer}>{stream.producer}</option>)}
                            </select></label>
                        </div>
                    )}
                    <details className="im-advanced">
                        <summary>Advanced options</summary>
                        <div className="im-actions im-advanced-fields">
                            <label>Section window <select value={sectionMode} onChange={event => setSectionMode(event.target.value)}>
                                <option value="">Default ({defaultSectionMode === 'sliding' ? 'Sliding' : 'Non-overlapping'})</option>
                                <option value="non_overlapping">Non-overlapping</option>
                                <option value="sliding">{videoMode ? 'Sliding (1-second step, up to 5 in parallel)' : 'Sliding (1-second step)'}</option>
                            </select></label>
                            <label>History <select value={historyMode} onChange={event => setHistoryMode(event.target.value)}>
                                <option value="">Default ({config?.settings.history_mode || 'recency'})</option>
                                <option value="recency">Recency</option><option value="uniform">Uniform</option>
                                <option value="no_memory">No memory</option>
                            </select></label>
                            {videoMode ? asset && <label>Takeoff offset in seconds (optional) <input type="number" min="0" max={asset.duration} step="0.1"
                                value={videoTakeoff} onChange={event => setVideoTakeoff(event.target.value)} /></label> :
                                <label>Takeoff time (optional) <input type="datetime-local" value={takeoff}
                                    aria-describedby="im-takeoff-help" onChange={event => setTakeoff(event.target.value)} /></label>}
                        </div>
                        {!videoMode && <p className="im-note" id="im-takeoff-help">Leave blank to start the timeline at the first valid frame after you start observing.</p>}
                    </details>
                    {config ? <ModelStages label="New run models" models={config.models} promptVersion={config.prompt_version} /> : <span className="im-muted">Loading model settings…</span>}
                    {config && !config.configured && <p className="im-note">Configure the vision model and its API key on the backend.</p>}
                </div>
                <div className="im-start-panel">
                    <Button type="button" label={videoMode ? 'Start analysis' : 'Start observing'}
                        icon="pi pi-play" onClick={start} disabled={!canStart} aria-describedby={startTargetId} />
                    <p className="im-muted" id={startTargetId}>{startTarget}</p>
                </div>
            </div>
            {error && renameValue === null && <p role="alert" className="im-error">{error}</p>}
            {session && <>
                {(!session.video || session.video_takeoff_offset != null) && (
                    <p className="im-run-summary" role="status">
                        {session.video_takeoff_offset != null ? 'Time relative to supplied takeoff offset' :
                            session.time_basis === 'takeoff' ? 'Time since takeoff' : 'Time since observation began'}
                        {active && session.source_type === 'live' && ` · ${session.source_notice}`}
                        {active && session.collecting_section != null && ` Section ${session.collecting_section} collecting (${session.collecting_frames} frames).`}
                        {active && frameAge != null && ` Last frame ${frameAge}s ago${frameAge > 5 ? ' (stale)' : ''}.`}
                    </p>
                )}
                {session.error && <p className="im-note">{session.error}</p>}
                <TimingMetrics label="Run timing" metrics={[
                    ['Run elapsed', runElapsed, 'Wall time since analysis started, including pauses'],
                    ['Avg. vision inference', averageVision, 'Average time to vision ready'],
                    ['Avg. section to DSL ready', averageDSL, 'Average time to compiled DSL'],
                ]} />
                <div className="im-report">
                    <aside className="im-timeline" aria-label="Section timeline">
                        <div className="im-detail-title"><h3>Timeline</h3>
                            {selectedIndex != null && <button type="button" onClick={() => setSelectedIndex(null)}>Follow latest</button>}
                        </div>
                        {sections.map(item => <button type="button" key={item.index} aria-current={item.index === index ? 'true' : undefined}
                            onClick={() => setSelectedIndex(item.index)} className="im-section">
                            <span><strong>#{String(item.index).padStart(3, '0')}</strong> {timestamp(item.start - (session.video_takeoff_offset || 0))}–{timestamp(item.end - (session.video_takeoff_offset || 0))}</span>
                            {item.status !== 'completed' && <span className="im-muted">{label(item.status)}</span>}
                            <span className="im-section-nl">{item.vision_inference || item.inference_text || item.error || 'Preparing section'}</span>
                            {item.vision_inference && item.vision_inference !== item.inference_text && item.inference_status === 'fallback'
                                && <span className="im-muted">Fallback mission</span>}
                            {(Number.isFinite(item.inference_elapsed) || compiledDSLReady(item)) && <span className="im-section-timing">
                                <span>Vision {durationLabel(item.inference_elapsed)}</span>
                                {compiledDSLReady(item) && <span>DSL ready {durationLabel(item.elapsed)}</span>}
                            </span>}
                        </button>)}
                        {!sections.length && <p className="im-muted">Waiting for the first section.</p>}
                    </aside>
                    {row ? <SectionDetail key={`${sessionId}/${row.index}`} row={row} session={session} onApply={onApplyArtifact} />
                        : <div className="im-empty">{summary ? 'Loading section…' : 'Section details will appear here.'}</div>}
                </div>
            </>}
        </section>
    );
}
