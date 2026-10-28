import React, { useState, useEffect, useCallback, useRef } from 'react';
import { TabView, TabPanel } from 'primereact/tabview';
import { ConfirmDialog } from 'primereact/confirmdialog';
import { Message } from 'primereact/message';
import PluginsTab from './PluginsTab.jsx';
import { daemonName, fetchDaemons } from './daemonsApi.js';

// Admin status, not live telemetry (contrast App.jsx's 500ms vehicle poll,
// which drives map markers): 5s keeps the daemons' status dots reasonably
// fresh.
const POLL_INTERVAL_MS = 5000;

// The daemon API only offers configuration on the GCS's own daemon and has no
// logs, so the Configure, Reset Config and Logs views (ConfigureDialog.jsx,
// LogViewer.jsx) are left out until it does.
const NOT_YET = "Configuration and logs aren't available through the daemon API yet.";

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
            {daemon.error && <Message severity="warn" text={`Unavailable: ${daemon.error}`} className="w-full mb-2" />}
            {rows.map(([label, value]) => (
                <div key={label} className="flex gap-2 text-sm py-1" style={{ borderBottom: '1px solid var(--surface-border)' }}>
                    <span className="text-color-secondary" style={{ width: '12rem', flexShrink: 0 }}>{label}</span>
                    <span style={{ fontFamily: 'monospace' }}>{value}</span>
                </div>
            ))}
            <div className="text-color-secondary text-sm mt-3">{NOT_YET}</div>
        </div>
    );
}

function StatusDot({ daemon }) {
    const color = daemon.error ? 'var(--red-500)' : 'var(--green-500)';
    return (
        <span
            title={daemon.error ? `Unavailable: ${daemon.error}` : 'Available'}
            style={{ width: '10px', height: '10px', borderRadius: '50%', flexShrink: 0, backgroundColor: color, border: `2px solid ${color}` }}
        />
    );
}

// The Orchestrate page: the daemons the GCS's daemon knows of (itself and its
// remotes), each with its info and services.
function OrchestrationPage({ toast }) {
    // daemons is null until the first list arrives; a failed poll keeps the
    // last list and shows why.
    const [daemons, setDaemons] = useState(null);
    const [error, setError] = useState(null);
    const [selectedIp, setSelectedIp] = useState(null);
    const [activeTab, setActiveTab] = useState(0);
    // Only the latest poll's answer is applied, when polls overlap.
    const latest = useRef(0);

    const refresh = useCallback(async () => {
        const request = ++latest.current;
        const result = await fetchDaemons();
        if (request !== latest.current) return;
        setError(result.error);
        if (result.daemons) setDaemons(result.daemons);
    }, []);

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        refresh();
        const intervalId = setInterval(refresh, POLL_INTERVAL_MS);
        return () => clearInterval(intervalId);
    }, [refresh]);

    const selected = daemons?.find((d) => d.ip === selectedIp) ?? null;

    return (
        <div className="p-2 flex gap-2" style={{ minHeight: '70vh' }}>
            <ConfirmDialog />
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
                                <StatusDot daemon={d} />
                                <div className="flex-1" style={{ minWidth: 0 }}>
                                    <div className="text-sm font-semibold white-space-nowrap overflow-hidden text-overflow-ellipsis">
                                        {daemonName(d)}{d.local ? ' (this GCS)' : ''}
                                    </div>
                                    <div className="text-xs text-color-secondary white-space-nowrap overflow-hidden text-overflow-ellipsis">
                                        {d.ip} · {d.services.length} service{d.services.length === 1 ? '' : 's'}
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
                        Select a daemon on the left to view its info and services.
                    </div>
                ) : (
                    <>
                        <div className="px-1 pb-2">
                            <span className="font-bold text-lg">{daemonName(selected)}</span>
                            <span className="text-color-secondary ml-2">{selected.ip}</span>
                        </div>
                        <TabView activeIndex={activeTab} onTabChange={(e) => setActiveTab(e.index)}>
                            <TabPanel header="Info">
                                <InfoPanel daemon={selected} />
                            </TabPanel>
                            <TabPanel header="Services">
                                <PluginsTab daemon={selected} toast={toast} onRestarted={refresh} />
                            </TabPanel>
                        </TabView>
                    </>
                )}
            </div>
        </div>
    );
}

export default React.memo(OrchestrationPage);
