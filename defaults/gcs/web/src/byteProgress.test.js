import { describe, it, expect } from 'vitest';
import { formatBytes, byteProgressLabel } from './byteProgress.js';

describe('formatBytes', () => {
    it('shows bytes under a KB', () => {
        expect(formatBytes(0)).toBe('0 B');
        expect(formatBytes(1023)).toBe('1023 B');
    });

    it('shows KB, MB and GB with one decimal', () => {
        expect(formatBytes(4198)).toBe('4.1 KB');
        expect(formatBytes(6 * 1024 * 1024)).toBe('6.0 MB');
        expect(formatBytes(13002342)).toBe('12.4 MB');
        expect(formatBytes(3 * 1024 ** 3)).toBe('3.0 GB');
    });
});

describe('byteProgressLabel', () => {
    it('shows done of total and the floored percentage', () => {
        expect(byteProgressLabel(6 * 1024 * 1024, 13002342)).toBe('6.0 MB / 12.4 MB · 48%');
    });

    it('is 0% before any total is known', () => {
        expect(byteProgressLabel(0, 0)).toBe('0 B / 0 B · 0%');
    });
});
