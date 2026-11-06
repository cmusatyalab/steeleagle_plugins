// Byte counts for upload progress, shared by the mission upload dialog and
// the Orchestrate page's Files tab.

const UNITS = ['KB', 'MB', 'GB', 'TB'];

// A byte count in B under a KB, else in the largest unit (1024-based) it
// reaches, to one decimal: "4.1 KB", "12.4 MB".
export function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    let value = n / 1024;
    let unit = 0;
    while (value >= 1024 && unit < UNITS.length - 1) {
        value /= 1024;
        unit++;
    }
    return `${value.toFixed(1)} ${UNITS[unit]}`;
}

// The floored percentage of total that done is, 0 before total is known.
export function bytePercent(done, total) {
    return total > 0 ? Math.floor((done * 100) / total) : 0;
}

// "6.0 MB / 12.4 MB · 48%"
export function byteProgressLabel(done, total) {
    return `${formatBytes(done)} / ${formatBytes(total)} · ${bytePercent(done, total)}%`;
}
