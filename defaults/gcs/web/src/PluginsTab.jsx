import React, { useState } from 'react';
import { DataTable } from 'primereact/datatable';
import { Column } from 'primereact/column';
import { Button } from 'primereact/button';
import { Tag } from 'primereact/tag';
import { confirmDialog } from 'primereact/confirmdialog';
import { restartService } from './daemonsApi.js';

// The Services tab: every service one daemon runs, from the daemons list the
// Orchestrate page polls, with its run state and a restart. The GCS's daemon
// forwards a restart to the daemon the service is on.
function PluginsTab({ daemon, toast, onRestarted }) {
    // The services being restarted, by full name: a restart can take tens of
    // seconds, and a second one while it runs would restart the service twice.
    const [restarting, setRestarting] = useState(() => new Set());

    // Not wrapped in useCallback: the React Compiler can't preserve a [toast]
    // dependency for toast.current, and it memoizes these itself.
    const restart = async (svc) => {
        if (restarting.has(svc.name)) return;
        setRestarting((prev) => new Set(prev).add(svc.name));
        const failure = await restartService(svc.name);
        setRestarting((prev) => {
            const next = new Set(prev);
            next.delete(svc.name);
            return next;
        });
        // Restarting the GCS drops this request, so a failure there is expected
        if (svc.self && failure) return;
        toast.current.show(failure
            ? { severity: 'error', summary: `Restart ${svc.bare}`, detail: failure }
            : { severity: 'success', summary: `Restart ${svc.bare}`, detail: 'Restarted' });
        if (!failure) onRestarted();
    };

    const confirmMessage = (svc) => {
        if (svc.self) return `Restart "${svc.bare}"? This is the GCS itself: this page will disconnect until it is back.`;
        if (svc.category === 'vehicle') return `Restart "${svc.bare}"? Do not restart a vehicle while it is flying or running a mission.`;
        return `Restart "${svc.bare}"?`;
    };

    const confirmRestart = (svc) => confirmDialog({
        message: confirmMessage(svc),
        header: 'Restart Service',
        icon: 'pi pi-refresh',
        acceptLabel: 'Restart',
        accept: () => restart(svc),
    });

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
            // so the restart spinner wouldn't show
            cellMemo={false}
        >
            <Column field="bare" header="Name" sortable />
            <Column field="category" header="Category" sortable body={(s) => <Tag value={s.category} severity="info" />} />
            <Column field="plugin" header="Plugin" sortable />
            <Column
                field="running"
                header="Status"
                sortable
                body={(s) => <Tag value={s.running ? 'Running' : 'Stopped'} severity={s.running ? 'success' : 'danger'} />}
            />
            <Column
                header=""
                style={{ width: '4rem' }}
                body={(s) => (
                    <Button
                        icon="pi pi-refresh"
                        size="small"
                        text
                        rounded
                        tooltip={`Restart ${s.bare}`}
                        tooltipOptions={{ position: 'top' }}
                        aria-label={`Restart ${s.bare}`}
                        loading={restarting.has(s.name)}
                        disabled={restarting.has(s.name)}
                        onClick={() => confirmRestart(s)}
                    />
                )}
            />
        </DataTable>
    );
}

export default PluginsTab;
