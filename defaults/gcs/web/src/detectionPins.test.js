import { describe, it, expect } from 'vitest';
import { classColor, pinOpacity, relativeTime, pinCardHTML, classCounts, visiblePins, PIN_FADE_START } from './detectionPins.js';

const pin = (extra = {}) => ({
    id: '1', engine: '100.64.0.1.yolo', class: 'car', lat: 40, long: -80,
    sightings: 47, first_seen_s: 125, last_seen_s: 4.2, vehicles: ['alpha', 'bravo'], ...extra,
});

describe('classColor', () => {
    it('is stable and differs across common classes', () => {
        expect(classColor('car')).toBe(classColor('car'));
        expect(classColor('car')).not.toBe(classColor('person'));
        expect(classColor('car')).toMatch(/^#[0-9a-f]{6}$/i);
    });
});

describe('pinOpacity', () => {
    it('holds then fades', () => {
        expect(PIN_FADE_START).toBe(0.5);
        expect(pinOpacity(0, 60)).toBe(1);
        expect(pinOpacity(30, 60)).toBe(1);
        expect(pinOpacity(60, 60)).toBeCloseTo(0.35);
        const mid = pinOpacity(45, 60);
        expect(mid).toBeGreaterThan(0.35);
        expect(mid).toBeLessThan(1);
        expect(pinOpacity(90, 60)).toBeCloseTo(0.35);
    });
});

describe('relativeTime', () => {
    it('floors to seconds, minutes or hours', () => {
        expect(relativeTime(4.2)).toBe('4 s ago');
        expect(relativeTime(125)).toBe('2 min ago');
        expect(relativeTime(3700)).toBe('1 h ago');
    });
});

describe('pinCardHTML', () => {
    it('escapes', () => {
        const html = pinCardHTML(pin({ class: '<img src=x onerror=alert(1)>', vehicles: ['<b>x</b>'] }));
        expect(html).not.toContain('<img');
        expect(html).not.toContain('<b>x');
        expect(html).toContain('&lt;img');
    });
    it('shows class, sightings, times, vehicles and engine on three lines, but no score', () => {
        const lines = pinCardHTML(pin()).split('<br>');
        expect(lines).toHaveLength(3);
        expect(lines[0]).toContain('<strong>car</strong> via yolo');
        expect(lines[1]).toContain('Seen: 47 times · By: alpha, bravo');
        expect(lines[2]).toContain('First: 2 min ago · Last: 4 s ago');
        expect(pinCardHTML(pin())).not.toMatch(/score|%/i);
    });
    it('is left-aligned', () => {
        expect(pinCardHTML(pin())).toContain('text-align:left');
    });
    it('says once for a single sighting', () => {
        expect(pinCardHTML(pin({ sightings: 1 }))).toContain('Seen: once');
    });
});

describe('classCounts and visiblePins', () => {
    const pins = [pin({ id: '1' }), pin({ id: '2', class: 'person' }), pin({ id: '3' })];
    it('counts by class, sorted', () => {
        expect(classCounts(pins)).toEqual([{ cls: 'car', count: 2 }, { cls: 'person', count: 1 }]);
    });
    it('filters hidden classes', () => {
        expect(visiblePins(pins, new Set(['car'])).map((p) => p.id)).toEqual(['2']);
        expect(visiblePins(pins, new Set())).toHaveLength(3);
    });
});
