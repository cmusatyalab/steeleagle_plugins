import { describe, it, expect } from 'vitest';
import { displayName, daemonOf } from './vehicleNames.js';

describe('displayName', () => {
    it('strips an IPv4 daemon prefix', () => {
        expect(displayName('100.64.1.5.cassowary')).toBe('cassowary');
    });
    it('strips an IPv6 daemon prefix', () => {
        expect(displayName('fd7a:115c::1.harpy')).toBe('harpy');
    });
    it('leaves a bare name alone', () => {
        expect(displayName('cassowary')).toBe('cassowary');
    });
    it('leaves a name without a valid address prefix alone', () => {
        expect(displayName('1.2.3.foo')).toBe('1.2.3.foo');
        expect(displayName('999.1.1.1.foo')).toBe('999.1.1.1.foo');
    });
    it('does not strip an address with no bare name after it', () => {
        expect(displayName('100.64.1.5.')).toBe('100.64.1.5.');
    });
});

describe('daemonOf', () => {
    it('returns the address prefix', () => {
        expect(daemonOf('100.64.1.5.cassowary')).toBe('100.64.1.5');
        expect(daemonOf('fd7a:115c::1.harpy')).toBe('fd7a:115c::1');
    });
    it('is empty without a valid prefix', () => {
        expect(daemonOf('cassowary')).toBe('');
        expect(daemonOf('1.2.3.foo')).toBe('');
    });
});
