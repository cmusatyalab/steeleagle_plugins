import React, { useState, useEffect, useCallback, useRef } from 'react';
import { TabView, TabPanel } from 'primereact/tabview';
import { ConfirmDialog, confirmDialog } from 'primereact/confirmdialog';
import { Button } from 'primereact/button';
import { Message } from 'primereact/message';
import ConfigureDialog from './ConfigureDialog.jsx';
import PackagesTab from './PackagesTab.jsx';
import PluginsTab from './PluginsTab.jsx';
import { daemonName, fetchDaemons, restartDaemon } from './daemonsApi.js';
import { applyOutcome, followStep, restartWarning } from './configForm.js';

// Admin status, not live telemetry (contrast App.jsx's 500ms vehicle poll,
// which drives map markers): 5s keeps the daemons' status dots reasonably
// fresh. While a daemon restarts, the page polls every second to see it
// come back.
const POLL_INTERVAL_MS = 5000;
const FOLLOW_POLL_INTERVAL_MS = 1000;

const STATE_COLORS = { up: 'var(--green-500)', services_failed: 'var(--orange-500)', unreachable: 'var(--red-500)' };

function stateText(daemon) {
    if (daemon.state === 'services_failed') return `Services failed to start: ${daemon.error}`;
    if (daemon.state === 'unreachable') return `Unavailable: ${daemon.error}`;
    return 'Available';
}

function InfoPanel({ daemon }) {
    const rows = [
        ['IP', daemon.ip],
        ['Hostname', daemon.hostname || '—'],
        ['Architecture', daemon.arch || '—'],
        ['OS', daemon.os || '—'],
        ['Role', daemon.local ? "This GCS's daemon (the orchestrator)" : 'Remote daemon'],
    ];
    return (
        <div className="p-2">
            {daemon.state !== 'up' && <Message severity="warn" text={stateText(daemon)} className="w-full mb-2" />}
            {rows.map(([label, value]) => (
                <div key={label} className="flex gap-2 text-sm py-1" style={{ borderBottom: '1px solid var(--surface-border)' }}>
                    <span className="text-color-secondary" style={{ width: '12rem', flexShrink: 0 }}>{label}</span>
                    <span style={{ fontFamily: 'monospace' }}>{value}</span>
                </div>
            ))}
            <div className="text-color-secondary text-sm mt-3">Logs aren't available through the daemon API yet.</div>
        </div>
    );
}

function StatusDot({ daemon, restarting }) {
    const color = restarting ? 'var(--blue-500)' : STATE_COLORS[daemon.state] ?? STATE_COLORS.unreachable;
    return (
        <span
            title={restarting ? 'Restarting' : stateText(daemon)}
            style={{ width: '10px', height: '10px', borderRadius: '50%', flexShrink: 0, backgroundColor: color, border: `2px solid ${color}` }}
        />
    );
}

// The Orchestrate page: the daemons the GCS's daemon knows of (itself and its
// remotes), each with its info, services and packages, and configuring or
// restarting it.
function OrchestrationPage({ toast }) {
    // daemons is null until the first list arrives; a failed poll keeps the
    // last list and shows why.
    const [daemons, setDaemons] = useState(null);
    const [error, setError] = useState(null);
    const [selectedIp, setSelectedIp] = useState(null);
    const [activeTab, setActiveTab] = useState(0);
    const [configuring, setConfiguring] = useState(null);
    // Daemons being followed after an apply or restart, by IP:
    // {startedAt, sawDown, name, own}
    const [follows, setFollows] = useState({});
    // Package changes made since each daemon's last restart, by IP:
    // {uri: 'install' | 'remove'}
    const [pending, setPending] = useState({});
    const latest = useRef(0);
    // The polls read the follows through a ref, so they needn't restart on
    // every change
    const followsRef = useRef(follows);
    useEffect(() => {
        followsRef.current = follows;
    }, [follows]);

    const settle = useCallback((list, listError) => {
        const now = Date.now();
        const current = followsRef.current;
        if (!Object.keys(current).length) return;
        const next = { ...current };
        for (const [ip, f] of Object.entries(current)) {
            const d = list?.find((x) => x.ip === ip) ?? null;
            const step = followStep(f, listError ? null : d, now);
            if (step.outcome === 'waiting') {
                next[ip] = { ...f, sawDown: step.sawDown };
                continue;
            }
            delete next[ip];
            if (step.outcome === 'back' || step.outcome === 'failed') {
                setPending((p) => {
                    const rest = { ...p };
                    delete rest[ip];
                    return rest;
                });
            }
            toast.current.show({
                back: { severity: 'success', summary: f.name, detail: `${f.name} is back.` },
                failed: { severity: 'error', summary: f.name, detail: `${f.name} is back, but its services failed to start: ${d?.error}`, sticky: true },
                timeout: { severity: 'warn', summary: f.name, detail: `${f.name} hasn't come back; check it on its host with eagle.`, sticky: true },
            }[step.outcome]);
        }
        setFollows(next);
    }, [toast]);

    const refresh = useCallback(async () => {
        const request = ++latest.current;
        const result = await fetchDaemons();
        if (request !== latest.current) return;
        // While the GCS's own daemon restarts, failing to list is expected
        const ownRestarting = Object.values(followsRef.current).some((f) => f.own);
        setError(result.error && !ownRestarting ? result.error : null);
        if (result.daemons) setDaemons(result.daemons);
        settle(result.daemons, result.error);
    }, [settle]);

    const following = Object.keys(follows).length > 0;
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        refresh();
        const intervalId = setInterval(refresh, following ? FOLLOW_POLL_INTERVAL_MS : POLL_INTERVAL_MS);
        return () => clearInterval(intervalId);
    }, [refresh, following]);

    const follow = (daemon) => setFollows((f) => ({
        ...f,
        [daemon.ip]: { startedAt: Date.now(), sawDown: false, name: daemonName(daemon), own: daemon.local },
    }));

    const doRestart = async (daemon) => {
        const result = await restartDaemon(daemon.ip);
        const outcome = applyOutcome(result, { own: daemon.local });
        if (outcome === 'applied' || outcome === 'restarting') {
            toast.current.show({ severity: 'info', summary: daemonName(daemon), detail: 'Restarting...' });
            follow(daemon);
        } else {
            toast.current.show({ severity: 'error', summary: `Restart ${daemonName(daemon)}`, detail: result.error });
        }
    };

    const confirmRestart = (daemon) => confirmDialog({
        message: restartWarning({ own: daemon.local }),
        header: `Restart ${daemonName(daemon)}`,
        icon: 'pi pi-exclamation-triangle',
        acceptLabel: 'Restart',
        acceptClassName: 'p-button-danger',
        accept: () => doRestart(daemon),
    });

    const selected = daemons?.find((d) => d.ip === selectedIp) ?? null;
    const selectedRestarting = selected ? !!follows[selected.ip] : false;
    const actionsDisabled = !selected || selected.state === 'unreachable' || selectedRestarting;

    return (
        <div className="p-2 flex gap-2" style={{ minHeight: '70vh' }}>
            <ConfirmDialog />
            {configuring && (
                <ConfigureDialog
                    daemon={configuring}
                    onHide={() => setConfiguring(null)}
                    onApplied={() => {
                        toast.current.show({ severity: 'info', summary: daemonName(configuring), detail: 'Config applied; restarting...' });
                        follow(configuring);
                        setConfiguring(null);
                    }}
                />
            )}
            <div className="flex flex-column gap-2" style={{ width: 280, flexShrink: 0 }}>
                <div className="border-round p-2" style={{ backgroundColor: 'var(--surface-card)', border: '1px solid var(--surface-border)' }}>
                    <div className="font-bold mb-2 px-1">Daemons</div>
                    {error && <Message severity="error" text={error} className="w-full mb-2" />}
                    {daemons === null && !error && <div className="text-color-secondary text-sm px-1 pb-2">Loading daemons...</div>}
                    <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
                        {(daemons ?? []).map((d) => (
                            <div
                                key={d.ip}
                                onClick={() => setSelectedIp(d.ip)}
                                className="flex align-items-center gap-2 p-2 border-round cursor-pointer mb-1"
                                style={{
                                    backgroundColor: d.ip === selectedIp ? 'var(--surface-hover)' : undefined,
                                    borderLeft: d.ip === selectedIp ? '3px solid var(--primary-color)' : '3px solid transparent',
                                }}
                            >
                                <StatusDot daemon={d} restarting={!!follows[d.ip]} />
                                <div className="flex-1" style={{ minWidth: 0 }}>
                                    <div className="text-sm font-semibold white-space-nowrap overflow-hidden text-overflow-ellipsis">
                                        {daemonName(d)}{d.local ? ' (this GCS)' : ''}
                                    </div>
                                    <div className="text-xs text-color-secondary white-space-nowrap overflow-hidden text-overflow-ellipsis">
                                        {d.ip} · {follows[d.ip] ? 'restarting' : `${d.services.length} service${d.services.length === 1 ? '' : 's'}`}
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            </div>
            <div className="flex-1 border-round p-2" style={{ backgroundColor: 'var(--surface-card)', border: '1px solid var(--surface-border)', minWidth: 0 }}>
                {!selected ? (
                    <div className="text-color-secondary flex align-items-center justify-content-center h-full">
                        Select a daemon on the left to view its info, services and packages.
                    </div>
                ) : (
                    <>
                        <div className="flex align-items-center justify-content-between px-1 pb-2 gap-2">
                            <span>
                                <span className="font-bold text-lg">{daemonName(selected)}</span>
                                <span className="text-color-secondary ml-2">{selected.ip}</span>
                            </span>
                            <span className="flex gap-2" title={selected.state === 'unreachable' ? stateText(selected) : undefined}>
                                <Button label="Configure..." icon="pi pi-cog" size="small" onClick={() => setConfiguring(selected)} disabled={actionsDisabled} />
                                <Button label="Restart Daemon" icon="pi pi-refresh" size="small" severity="danger" outlined onClick={() => confirmRestart(selected)} disabled={actionsDisabled} />
                            </span>
                        </div>
                        <TabView activeIndex={activeTab} onTabChange={(e) => setActiveTab(e.index)}>
                            <TabPanel header="Info">
                                <InfoPanel daemon={selected} />
                            </TabPanel>
                            <TabPanel header="Services">
                                <PluginsTab daemon={selected} toast={toast} onRestarted={refresh} />
                            </TabPanel>
                            <TabPanel header="Packages">
                                <PackagesTab
                                    key={selected.ip}
                                    daemon={selected}
                                    toast={toast}
                                    pending={pending[selected.ip] ?? {}}
                                    onPending={(uri, action) => setPending((p) => ({ ...p, [selected.ip]: { ...(p[selected.ip] ?? {}), [uri]: action } }))}
                                    onRestartNow={() => confirmRestart(selected)}
                                    disabled={actionsDisabled}
                                />
                            </TabPanel>
                        </TabView>
                    </>
                )}
            </div>
        </div>
    );
}

export default React.memo(OrchestrationPage);
