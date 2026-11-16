import React, { useState } from 'react';
import { DataTable } from 'primereact/datatable';
import { Column } from 'primereact/column';
import { Button } from 'primereact/button';
import { Tag } from 'primereact/tag';
import { confirmDialog } from 'primereact/confirmdialog';
import { restartService, stopService } from './daemonsApi.js';

// A service's status: running, stopped by request (until it is started
// again), or stopped because it exited or failed to start.
function statusTag(svc) {
    if (svc.running) return <Tag value="Running" severity="success" />;
    if (svc.stopped) return <Tag value="Stopped by request" severity="secondary" />;
    return <Tag value="Stopped" severity="danger" />;
}

// The Services tab: every service one daemon runs, from the daemons list the
// Orchestrate page polls, with its run state, a restart, a stop (which lasts
// across its daemon's restarts until it is started again), and a shortcut to
// its card in the Configure dialog (onConfigure). The GCS's daemon forwards
// these to the daemon the service is on. The GCS can't be stopped from here,
// since nothing could start it again.
function PluginsTab({ daemon, toast, onRestarted, onConfigure, configureDisabled }) {
    // The services being stopped, started or restarted, by full name: each
    // can take tens of seconds, and a second one while it runs would act on
    // the service twice.
    const [busy, setBusy] = useState(() => new Set());

    // Runs one action on a service: call (the API function), then a toast
    // titled `${verb} ${bare}` saying done, or why not.
    // Not wrapped in useCallback: the React Compiler can't preserve a [toast]
    // dependency for toast.current, and it memoizes these itself.
    const runAction = async (svc, call, verb, done) => {
        if (busy.has(svc.name)) return;
        setBusy((prev) => new Set(prev).add(svc.name));
        const failure = await call(svc.name);
        setBusy((prev) => {
            const next = new Set(prev);
            next.delete(svc.name);
            return next;
        });
        // Restarting the GCS drops this request, so a failure there is expected
        if (svc.self && failure) return;
        toast.current.show(failure
            ? { severity: 'error', summary: `${verb} ${svc.bare}`, detail: failure }
            : { severity: 'success', summary: `${verb} ${svc.bare}`, detail: done });
        if (!failure) onRestarted();
    };

    const restart = (svc) => runAction(svc, restartService, 'Restart', 'Restarted');
    const start = (svc) => runAction(svc, restartService, 'Start', 'Started');
    const stop = (svc) => runAction(svc, stopService, 'Stop', 'Stopped until started again');

    const confirmMessage = (svc, verb) => {
        if (svc.self) return `${verb} "${svc.bare}"? This is the GCS itself: this page will disconnect until it is back.`;
        if (svc.category === 'vehicle') return `${verb} "${svc.bare}"? Do not ${verb.toLowerCase()} a vehicle while it is flying or running a mission.`;
        return `${verb} "${svc.bare}"?`;
    };

    const confirmRestart = (svc) => confirmDialog({
        message: confirmMessage(svc, 'Restart'),
        header: 'Restart Service',
        icon: 'pi pi-refresh',
        acceptLabel: 'Restart',
        accept: () => restart(svc),
    });

    const confirmStop = (svc) => confirmDialog({
        message: `${confirmMessage(svc, 'Stop')} It stays stopped, even when its daemon restarts, until you start it again.`,
        header: 'Stop Service',
        icon: 'pi pi-stop-circle',
        acceptLabel: 'Stop',
        acceptClassName: 'p-button-danger',
        accept: () => stop(svc),
    });

    // A small round icon button for one of a service's actions
    const actionButton = (svc, icon, label, onClick, extra = {}) => (
        <Button
            icon={icon}
            size="small"
            text
            rounded
            tooltip={label}
            tooltipOptions={{ position: 'top' }}
            aria-label={label}
            loading={busy.has(svc.name)}
            disabled={busy.has(svc.name)}
            onClick={onClick}
            {...extra}
        />
    );

    return (
        <DataTable
            value={daemon.services}
            emptyMessage={{
                services_failed: `This daemon's services failed to start: ${daemon.error}. Fix its config with Configure.`,
                unreachable: `Daemon is unavailable: ${daemon.error}`,
            }[daemon.state] ?? 'This daemon runs no services.'}
            size="small"
            dataKey="name"
            sortField="bare"
            sortOrder={1}
            // The table otherwise redraws a cell only when its row changes,
            // so the action spinners wouldn't show
            cellMemo={false}
        >
            <Column field="bare" header="Name" sortable />
            <Column field="category" header="Category" sortable body={(s) => <Tag value={s.category} severity="info" />} />
            <Column field="plugin" header="Plugin" sortable />
            <Column
                field="running"
                header="Status"
                sortable
                body={statusTag}
            />
            <Column
                header=""
                style={{ width: '8rem' }}
                body={(s) => (
                    <span className="flex">
                        <Button
                            icon="pi pi-cog"
                            size="small"
                            text
                            rounded
                            tooltip={`Configure ${s.bare}`}
                            tooltipOptions={{ position: 'top' }}
                            aria-label={`Configure ${s.bare}`}
                            disabled={configureDisabled}
                            onClick={() => onConfigure(s)}
                        />
                        {s.stopped ? (
                            // A stopped service is started, not restarted
                            actionButton(s, 'pi pi-play', `Start ${s.bare}`, () => start(s), { severity: 'success' })
                        ) : (
                            <>
                                {!s.self && actionButton(s, 'pi pi-stop-circle', `Stop ${s.bare}`, () => confirmStop(s), { severity: 'danger' })}
                                {actionButton(s, 'pi pi-refresh', `Restart ${s.bare}`, () => confirmRestart(s))}
                            </>
                        )}
                    </span>
                )}
            />
        </DataTable>
    );
}

export default PluginsTab;
