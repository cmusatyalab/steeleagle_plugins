import React, { useRef, useState } from 'react';
import { Button } from 'primereact/button';
import { InputText } from 'primereact/inputtext';
import { Message } from 'primereact/message';
import { ProgressBar } from 'primereact/progressbar';
import { DataTable } from 'primereact/datatable';
import { Column } from 'primereact/column';
import { byteProgressLabel, bytePercent, formatBytes } from './byteProgress.js';
import { validFileName } from './fileTransfer.js';

const KINDS = [
    { kind: 'file', label: 'File', icon: 'pi pi-file' },
    { kind: 'model', label: 'Model', icon: 'pi pi-box' },
];

function progressCell(u) {
    switch (u.status) {
        case 'done': return <span style={{ color: 'var(--green-500)' }}>✓ {formatBytes(u.size)} saved</span>;
        case 'failed': return <span style={{ color: 'var(--red-500)' }}>✗ {u.detail || 'Failed'}</span>;
        case 'cancelled': return <span className="text-color-secondary">Cancelled</span>;
        default: return (
            <span className="flex align-items-center gap-2">
                <ProgressBar className="byte-progress" value={bytePercent(u.sent, u.size)} showValue={false} style={{ height: '0.6rem', width: '6rem', flexShrink: 0 }} />
                <span className="text-sm white-space-nowrap">{byteProgressLabel(u.sent, u.size)}</span>
            </span>
        );
    }
}

// The Files tab: uploading files and models to one daemon with the daemon's
// TransferFile and TransferModel. The daemon can't list or delete them yet,
// so the table holds only the uploads made from this page since it opened
// (uploads, newest first); onStart(kind, name, file) starts one and
// onCancel(id) cancels one.
function FilesTab({ daemon, uploads, onStart, onCancel, disabled }) {
    const [kind, setKind] = useState('file');
    const [file, setFile] = useState(null);
    const [name, setName] = useState('');
    const [dragging, setDragging] = useState(false);
    const input = useRef(null);

    const pick = (f) => {
        if (!f) return;
        setFile(f);
        setName(f.name);
    };

    const nameError = file ? validFileName(name) : null;
    const busy = uploads.some((u) => u.status === 'uploading');
    const canUpload = file && !nameError && !busy && !disabled;

    const upload = () => {
        onStart(kind, name, file);
        setFile(null);
        setName('');
        if (input.current) input.current.value = '';
    };

    return (
        <div className="flex flex-column gap-2">
            <div className="text-sm text-color-secondary">
                Files are saved to <code>steeleagle/files/</code> and models to <code>steeleagle/models/</code>, which
                every engine can read, under the daemon's XDG data home. Uploading a name that already exists replaces it.
            </div>
            <div className="flex align-items-center gap-2">
                <span className="text-sm white-space-nowrap">Upload as:</span>
                <span className="p-buttonset">
                    {KINDS.map((k) => (
                        <Button key={k.kind} label={k.label} icon={k.icon} size="small"
                            outlined={kind !== k.kind} onClick={() => setKind(k.kind)} />
                    ))}
                </span>
            </div>
            <div
                role="button"
                tabIndex={0}
                onClick={() => input.current?.click()}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') input.current?.click(); }}
                onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => { e.preventDefault(); setDragging(false); pick(e.dataTransfer.files[0]); }}
                className="flex align-items-center justify-content-center border-round cursor-pointer text-color-secondary"
                style={{
                    height: '5rem',
                    border: `2px dashed ${dragging ? 'var(--primary-color)' : 'var(--surface-border)'}`,
                    backgroundColor: dragging ? 'var(--surface-hover)' : undefined,
                }}
            >
                {file
                    ? <span><i className="pi pi-file mr-2" />{file.name} ({formatBytes(file.size)})</span>
                    : <span><i className="pi pi-upload mr-2" />Drop a file here or click to choose</span>}
            </div>
            <input ref={input} type="file" hidden onChange={(e) => pick(e.target.files[0])} />
            <div className="flex align-items-center gap-2">
                <span className="text-sm white-space-nowrap">Save as:</span>
                <InputText value={name} onChange={(e) => setName(e.target.value)} aria-label="Save as"
                    disabled={!file} invalid={!!nameError} className="p-inputtext-sm" style={{ width: '24rem' }} />
                <Button label="Upload" icon="pi pi-upload" size="small" onClick={upload} disabled={!canUpload}
                    tooltip={busy ? 'Wait for the upload to this daemon to finish' : undefined} tooltipOptions={{ showOnDisabled: true, position: 'top' }} />
            </div>
            {nameError && <Message severity="error" text={`The name must be ${nameError}.`} className="w-full" />}
            <div className="font-semibold mt-2">Uploaded this session</div>
            <DataTable value={uploads} size="small" dataKey="id" cellMemo={false}
                emptyMessage={`Nothing uploaded to ${daemon.ip} since this page opened.`}>
                <Column field="name" header="Name" body={(u) => <span style={{ fontFamily: 'monospace' }}>{u.name}</span>} />
                <Column field="kind" header="Kind" />
                <Column header="Progress" body={progressCell} />
                <Column header="" style={{ width: '7rem' }} body={(u) => (u.status === 'uploading'
                    ? <Button label="Cancel" icon="pi pi-times" size="small" text severity="danger" onClick={() => onCancel(u.id)} />
                    : null)} />
            </DataTable>
        </div>
    );
}

export default FilesTab;
