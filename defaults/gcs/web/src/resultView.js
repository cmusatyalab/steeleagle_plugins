// What the video panel shows of engines' results: which engines can be picked,
// and how their results are drawn. Results come from the GCS's data channel as
// protobuf JSON (rtc.DataMessage), so fields are lowerCamelCase.
import { orchestrator, shortType } from './flowCatalog.js';
import { displayName } from './vehicleNames.js';

// The result types the panel can show; a GenericResult has no known shape.
export const DISPLAYABLE_RESULTS = ['DetectionResult', 'ClassificationResult', 'GuidanceResult', 'PositionResult'];

// How long a result stays on screen without a newer one.
export const RESULT_TTL_MS = 2000;

// The engines on the GCS's own machine whose results can be shown, as
// dropdown options {value, label, type}, sorted by label. The GCS can only
// subscribe to the engines of its own daemon. `selected` is kept as an option
// even if it is no longer listed, so the dropdown never silently clears.
export function resultEngineOptions(catalog, selected) {
  const engines = orchestrator(catalog)?.engines ?? [];
  const options = [];
  for (const e of engines) {
    const type = shortType(e.result || e.events?.find((ev) => ev.label === 'result')?.type || null);
    if (!DISPLAYABLE_RESULTS.includes(type)) continue;
    options.push({ value: e.name, label: `${displayName(e.name)} (${type})`, type });
  }
  options.sort((a, b) => a.label.localeCompare(b.label));
  if (selected && !options.some((o) => o.value === selected)) {
    options.push({ value: selected, label: `${displayName(selected)} (not running)`, type: null });
  }
  return options;
}

// A detection result's boxes in pixels of a w x h image:
// [{x, y, width, height, label, color}]. Boxes are clamped to the image.
export function detectionBoxes(result, w, h) {
  const boxes = [];
  for (const d of result?.detections ?? []) {
    const b = d.bbox ?? {};
    const clamp = (v) => Math.min(Math.max(Number(v) || 0, 0), 1);
    const x0 = clamp(b.xMin) * w;
    const y0 = clamp(b.yMin) * h;
    const x1 = clamp(b.xMax) * w;
    const y1 = clamp(b.yMax) * h;
    if (x1 <= x0 || y1 <= y0) continue;
    const name = d.className || 'object';
    const score = typeof d.score === 'number' ? ` ${Math.round(d.score * 100)}%` : '';
    boxes.push({ x: x0, y: y0, width: x1 - x0, height: y1 - y0, label: `${name}${score}`, color: classColor(name) });
  }
  return boxes;
}

// A stable color for a class name, so each class keeps its color.
export function classColor(name) {
  let hash = 0;
  for (const c of name) hash = (hash * 31 + c.codePointAt(0)) >>> 0;
  return `hsl(${hash % 360}, 90%, 55%)`;
}
