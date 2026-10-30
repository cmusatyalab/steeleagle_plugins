import { describe, it, expect } from 'vitest';
import { classColor, detectionBoxes, resultEngineOptions } from './resultView.js';

const R = 'steeleagle_protocol/v1/messages/result';
const engine = (name, result) => ({ name, result, events: [{ label: 'result', type: result || null }] });
const catalog = {
  daemons: [
    {
      ip: '10.0.0.1', local: true, vehicles: [], engines: [
        engine('10.0.0.1.yolo', `${R}/DetectionResult`),
        engine('10.0.0.1.slam', `${R}/PositionResult`),
        engine('10.0.0.1.score', 'messages/result/GenericResult'),
        engine('10.0.0.1.encoder', ''),
        engine('10.0.0.1.avoid', `${R}/GuidanceResult`),
        engine('10.0.0.1.cls', `${R}/ClassificationResult`),
      ],
    },
    { ip: '10.0.0.2', local: false, vehicles: [], engines: [engine('10.0.0.2.yolo', `${R}/DetectionResult`)] },
  ],
  unavailable: [],
};

describe('resultEngineOptions', () => {
  it('lists the local engines with displayable results, with their type', () => {
    expect(resultEngineOptions(catalog, null)).toEqual([
      { value: '10.0.0.1.avoid', label: 'avoid (GuidanceResult)', type: 'GuidanceResult' },
      { value: '10.0.0.1.cls', label: 'cls (ClassificationResult)', type: 'ClassificationResult' },
      { value: '10.0.0.1.slam', label: 'slam (PositionResult)', type: 'PositionResult' },
      { value: '10.0.0.1.yolo', label: 'yolo (DetectionResult)', type: 'DetectionResult' },
    ]);
  });
  it('falls back to the result event type', () => {
    const c = { daemons: [{ ip: '1.2.3.4', local: true, engines: [{ name: '1.2.3.4.d', events: [{ label: 'result', type: `${R}/DetectionResult` }] }] }] };
    expect(resultEngineOptions(c, null).map((o) => o.value)).toEqual(['1.2.3.4.d']);
  });
  it('keeps a selected engine that is gone', () => {
    const options = resultEngineOptions({ daemons: [], unavailable: [] }, '10.0.0.1.yolo');
    expect(options).toEqual([{ value: '10.0.0.1.yolo', label: 'yolo (not running)', type: null }]);
  });
});

describe('detectionBoxes', () => {
  it('scales boxes to pixels and labels them with class and score', () => {
    const result = { detections: [{ className: 'car', score: 0.876, bbox: { xMin: 0.1, yMin: 0.2, xMax: 0.5, yMax: 0.6 } }] };
    const [b] = detectionBoxes(result, 1000, 500);
    expect(b.x).toBeCloseTo(100);
    expect(b.y).toBeCloseTo(100);
    expect(b.width).toBeCloseTo(400);
    expect(b.height).toBeCloseTo(200);
    expect(b.label).toBe('car 88%');
    expect(b.color).toBe(classColor('car'));
  });
  it('clamps boxes to the image and skips empty ones', () => {
    const result = { detections: [
      { className: 'a', bbox: { xMin: -0.5, yMin: 0, xMax: 2, yMax: 1 } },
      { className: 'b', bbox: { xMin: 0.5, yMin: 0.5, xMax: 0.5, yMax: 0.9 } },
      { className: 'c' },
    ] };
    const boxes = detectionBoxes(result, 100, 100);
    expect(boxes).toHaveLength(1);
    expect(boxes[0]).toMatchObject({ x: 0, y: 0, width: 100, height: 100, label: 'a' });
  });
  it('handles no detections', () => {
    expect(detectionBoxes({ detections: [] }, 10, 10)).toEqual([]);
    expect(detectionBoxes(null, 10, 10)).toEqual([]);
  });
});
