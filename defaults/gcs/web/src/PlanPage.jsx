import { useState, useCallback, useRef, useEffect, useMemo } from 'react';
import { TabView, TabPanel } from 'primereact/tabview';
import { Button } from 'primereact/button';
import { SplitButton } from 'primereact/splitbutton';
import { Toast } from 'primereact/toast';
import { InputTextarea } from 'primereact/inputtextarea';
import { Dialog } from 'primereact/dialog';
import { MultiSelect } from 'primereact/multiselect';
import { InputText } from 'primereact/inputtext';
import { streamUpload, initialUploadState, applyUploadEvent, isUploadFinished, finishUpload, validMissionName } from './missionUpload.js';
import {
    ReactFlow, Background, Controls, MiniMap,
    applyNodeChanges, applyEdgeChanges, addEdge,
    useReactFlow, ReactFlowProvider, MarkerType,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import MapDraw from './MapDraw.jsx';
import TaskNode from './TaskNode.jsx';
import SelfLoopEdge from './SelfLoopEdge.jsx';
import TaskNodePanel from './TaskNodePanel.jsx';
import EdgePanel from './EdgePanel.jsx';
import FsmPalette from './FsmPalette.jsx';
import ConnectModal from './ConnectModal.jsx';
import CanvasContextMenu from './CanvasContextMenu.jsx';
import FlowBuilder from './FlowBuilder.jsx';
import VehiclePicker from './VehiclePicker.jsx';
import { canvasThemeVars } from './canvasTheme.js';
import { getApiUrl } from './urls.js';
import { runValidation } from './validation.js';
import { missionRequestBody, renderDsl } from './missionGraph.js';
import { OLD_VERSION_MESSAGE, emptyFlow, parseFlowJson, serializeFlow, toTemplate, validateFlow } from './flowModel.js';
import { engineChoices } from './flowEngines.js';
import { loadMissionDraft, saveMissionDraft, highestNodeNumber } from './missionDraft.js';

const FLOW_DRAFT_KEY = 'steeleagle-flow-draft';

// The Flow Builder draft survives tab switches and reloads; it's a
// convenience, not storage -- the exported file is the saved flow. Returns
// {flow, notice}: notice is set when the draft was saved by an older Flow
// Builder, so the page can say why the builder started empty.
function loadFlowDraft() {
    try {
        const raw = localStorage.getItem(FLOW_DRAFT_KEY);
        return { flow: raw ? parseFlowJson(raw) : emptyFlow(), notice: null };
    } catch (e) {
        return { flow: emptyFlow(), notice: e.message === OLD_VERSION_MESSAGE ? OLD_VERSION_MESSAGE : null };
    }
}

function saveFlowDraft(flow) {
    try {
        localStorage.setItem(FLOW_DRAFT_KEY, serializeFlow(flow));
    } catch {
        // storage full or blocked; the flow still lives in page state
    }
}

const nodeTypes = { taskNode: TaskNode };
const edgeTypes = { selfLoop: SelfLoopEdge };

// Event transitions are animated; "done" is static, so it gets an arrowhead to show direction.
function edgeAppearance(eventId) {
    const isDone = eventId === 'done';
    const color = isDone ? '#a3e8a0' : '#c47aff';
    return {
        animated: !isDone,
        style: { stroke: color },
        labelStyle: { fill: color, fontSize: 10 },
        ...(isDone && { markerEnd: { type: MarkerType.ArrowClosed, color, width: 18, height: 18 } }),
    };
}

let _nodeIdCounter = 1;
function nextNodeId() { return `node-${_nodeIdCounter++}`; }

// BFS layout: place nodes in grid columns by distance from start
function bfsLayout(nodeIds, startId, edges) {
    const adj = {};
    nodeIds.forEach(id => { adj[id] = []; });
    edges.forEach(e => {
        if (e.source !== e.target && adj[e.source]) adj[e.source].push(e.target);
    });

    const visited = new Set();
    const levels = {};
    const queue = startId ? [startId] : nodeIds.slice(0, 1);
    let level = 0;
    queue.forEach(id => { visited.add(id); levels[id] = 0; });

    while (queue.length) {
        const next = [];
        queue.forEach(id => {
            (adj[id] || []).forEach(t => {
                if (!visited.has(t)) { visited.add(t); levels[t] = level + 1; next.push(t); }
            });
        });
        queue.length = 0;
        queue.push(...next);
        if (next.length) level++;
    }
    // any unreachable nodes get appended after
    nodeIds.forEach(id => { if (!(id in levels)) levels[id] = level + 1; });

    const colCounts = {};
    const positions = {};
    nodeIds.forEach(id => {
        const col = levels[id] ?? 0;
        const row = colCounts[col] ?? 0;
        colCounts[col] = row + 1;
        positions[id] = { x: col * 260, y: row * 130 };
    });
    return positions;
}

// Coerces a FieldSchema's raw string default (the proto's wire type,
// regardless of the field's own kind) to the JS type params actually
// need: a number for number/integer fields, a boolean for boolean
// fields, the string as-is otherwise (including enum idents and
// map_feature area names, which are genuinely strings).
function coerceDefault(field) {
    if (field.type === 'number' || field.type === 'integer') {
        const n = Number(field.default);
        return Number.isNaN(n) ? field.default : n;
    }
    if (field.type === 'boolean') return field.default === 'true';
    return field.default;
}

// Extract named areas from GeoJSON features string
function getNamedAreas(featuresStr) {
    try {
        const fc = typeof featuresStr === 'string' ? JSON.parse(featuresStr) : featuresStr;
        return (fc.features || [])
            .map(f => f.properties?.name)
            .filter(Boolean);
    } catch { return []; }
}

function FsmCanvas({ nodes, edges, setNodes, setEdges, eventInstances, setEventInstances,
                     startNodeId, setStartNodeId, schema, features, setPanelNode,
                     setPanelEdgeId, pushSnapshot, theme, engines, active = true }) {
    const { screenToFlowPosition } = useReactFlow();
    const [connectModal, setConnectModal] = useState({ visible: false, connection: null });
    const [contextMenu, setContextMenu] = useState(null);
    const namedAreas = getNamedAreas(features);

    const onNodesChange = useCallback((changes) => setNodes(ns => applyNodeChanges(changes, ns)), [setNodes]);
    const onEdgesChange = useCallback((changes) => setEdges(es => applyEdgeChanges(changes, es)), [setEdges]);

    // Called when a connection is drawn between two handles (including self-loops)
    const onConnect = useCallback((connection) => {
        setConnectModal({ visible: true, connection });
    }, []);

    const onEdgeClick = useCallback((event, edge) => {
        setPanelEdgeId(edge.id);
    }, [setPanelEdgeId]);

    function confirmConnect(connection, eventId) {
        pushSnapshot();
        const isSelfLoop = connection.source === connection.target;
        setEdges(es => addEdge({
            ...connection,
            type: isSelfLoop ? 'selfLoop' : 'smoothstep',
            data: { eventId },
            label: eventId,
            ...edgeAppearance(eventId),
        }, es));
    }

    // Drag from palette onto canvas
    const onDragOver = useCallback((event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
    }, []);

    const onDrop = useCallback((event) => {
        event.preventDefault();
        const typeName = event.dataTransfer.getData('application/reactflow/typeName');
        if (!typeName) return;

        const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
        const id = nextNodeId();
        const isFirst = nodes.length === 0;

        // Build defaults from schema. field.default is always a raw string
        // (FieldSchema.default_value's wire type) regardless of the
        // field's actual kind -- coerce it to match, or a number/boolean
        // field silently carries a string default all the way to /api/build,
        // which fails with a Go type error ("10.0" isn't a float32) that
        // Validate's lighter checking never catches.
        const fields = schema.actions[typeName]?.fields ?? [];
        const defaultParams = Object.fromEntries(
            fields.filter(f => 'default' in f).map(f => [f.name, coerceDefault(f)])
        );

        const base = typeName.replace(/([A-Z])/g, '_$1').toLowerCase().replace(/^_/, '');
        const count = nodes.filter(n => n.data.type_name === typeName).length;
        const instanceId = `${base}_${count + 1}`;

        const newNode = {
            id,
            type: 'taskNode',
            position,
            data: {
                type_name: typeName,
                instance_id: instanceId,
                params: defaultParams,
                isStart: isFirst,
                schema: schema.actions[typeName],
                namedAreas,
                onUpdate: (params) => setNodes(ns => ns.map(n => n.id === id ? { ...n, data: { ...n.data, params } } : n)),
                onUpdateId: (newId) => setNodes(ns => ns.map(n => n.id === id ? { ...n, data: { ...n.data, instance_id: newId } } : n)),
                onOpenPanel: () => setPanelNode(id),
            },
        };

        pushSnapshot();
        setNodes(ns => [...ns, newNode]);
        if (isFirst) setStartNodeId(id);
    }, [nodes, schema, namedAreas, screenToFlowPosition, pushSnapshot, setNodes, setPanelNode, setStartNodeId]);

    // Right-click context menu on a node
    const onNodeContextMenu = useCallback((event, node) => {
        event.preventDefault();
        setContextMenu({ x: event.clientX, y: event.clientY, nodeId: node.id });
    }, []);

    // Right-click context menu on an edge
    const onEdgeContextMenu = useCallback((event, edge) => {
        event.preventDefault();
        setContextMenu({ x: event.clientX, y: event.clientY, edgeId: edge.id });
    }, []);

    function setAsStart(nodeId) {
        pushSnapshot();
        setStartNodeId(nodeId);
        setNodes(ns => ns.map(n => ({ ...n, data: { ...n.data, isStart: n.id === nodeId } })));
        setContextMenu(null);
    }

    function deleteNode(nodeId) {
        pushSnapshot();
        setNodes(ns => ns.filter(n => n.id !== nodeId));
        setEdges(es => es.filter(e => e.source !== nodeId && e.target !== nodeId));
        if (startNodeId === nodeId) setStartNodeId(null);
        setContextMenu(null);
    }

    // Sync namedAreas into all node data when features change
    useEffect(() => {
        setNodes(ns => ns.map(n => ({ ...n, data: { ...n.data, namedAreas } })));
    }, [features]);

    return (
        <div style={{ flex: 1, position: 'relative' }} onClick={() => setContextMenu(null)}>
            <ReactFlow
                colorMode={theme}
                deleteKeyCode={active ? 'Backspace' : null}
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onEdgeClick={onEdgeClick}
                onDragOver={onDragOver}
                onDrop={onDrop}
                onNodeContextMenu={onNodeContextMenu}
                onEdgeContextMenu={onEdgeContextMenu}
                fitView
            >
                <Controls />
                <MiniMap />
                <Background variant="dots" gap={12} size={1} />
            </ReactFlow>

            <CanvasContextMenu
                position={contextMenu}
                onClose={() => setContextMenu(null)}
                items={contextMenu?.nodeId ? [
                    { label: '▶ Set as Start State', onClick: () => setAsStart(contextMenu.nodeId) },
                    { label: '🗑 Delete node', danger: true, onClick: () => deleteNode(contextMenu.nodeId) },
                ] : [
                    { label: '✏ Edit transition', onClick: () => setPanelEdgeId(contextMenu?.edgeId) },
                    { label: '🗑 Delete transition', danger: true, onClick: () => { pushSnapshot(); setEdges(es => es.filter(e => e.id !== contextMenu.edgeId)); } },
                ]}
            />

            <ConnectModal
                visible={connectModal.visible}
                onHide={() => setConnectModal({ visible: false, connection: null })}
                connection={connectModal.connection}
                eventInstances={eventInstances}
                schema={schema}
                engines={engines}
                onConfirm={confirmConnect}
                onAddEvent={(ev) => setEventInstances(evs => [...evs, ev])}
            />
        </div>
    );
}

function PlanPage({ theme, vehicles = [], squadList, uploadState, setUploadState, visible = true }) {
    const [activeIndex, setActiveIndex] = useState(0);
    const draftNotice = useRef(null);
    const [flow, setFlow] = useState(() => {
        const draft = loadFlowDraft();
        draftNotice.current = draft.notice;
        return draft.flow;
    });
    const [catalog, setCatalog] = useState(null);
    const [panelNodeId, setPanelNodeId] = useState(null);
    // Nodes keep a callback and their type's schema in data; neither is
    // stored, so put them back on nodes coming from a draft or snapshot.
    const schemaRef = useRef(null);
    const hydrateNodes = (ns) => ns.map(n => ({
        ...n,
        data: {
            ...n.data,
            schema: schemaRef.current?.actions[n.data.type_name],
            onOpenPanel: () => setPanelNodeId(n.id),
        },
    }));
    const [missionDraft] = useState(() => {
        const draft = loadMissionDraft();
        _nodeIdCounter = Math.max(_nodeIdCounter, highestNodeNumber(draft.nodes) + 1);
        return draft;
    });
    const [nodes, setNodes] = useState(() => hydrateNodes(missionDraft.nodes));
    const [edges, setEdges] = useState(missionDraft.edges);
    const [eventInstances, setEventInstances] = useState(missionDraft.eventInstances);
    const [startNodeId, setStartNodeId] = useState(missionDraft.startNodeId);
    const [schema, setSchema] = useState({ actions: {}, events: {}, enums: {} });
    const [missionCompiled, setMissionCompiled] = useState(false);
    const [features, setFeatures] = useState(missionDraft.features);
    const panelNode = panelNodeId ? nodes.find(n => n.id === panelNodeId) : null;
    const [panelEdgeId, setPanelEdgeId] = useState(null);
    const panelEdge = panelEdgeId ? edges.find(e => e.id === panelEdgeId) : null;
    const panelEventInstance = panelEdge ? eventInstances.find(ev => ev.instance_id === panelEdge.data?.eventId) : null;
    const panelEdgeSourceLabel = panelEdge ? (nodes.find(n => n.id === panelEdge.source)?.data.instance_id ?? panelEdge.source) : '';
    const panelEdgeTargetLabel = panelEdge ? (nodes.find(n => n.id === panelEdge.target)?.data.instance_id ?? panelEdge.target) : '';
    const [compiling, setCompiling] = useState(false);
    const [building, setBuilding] = useState(null); // arch currently downloading, or null
    const [loadingDsl, setLoadingDsl] = useState(false);
    const [applyingDsl, setApplyingDsl] = useState(false);
    const [dslEdited, setDslEdited] = useState(false);
    const [dslText, setDslText] = useState('');
    const fileInputRef = useRef(null);
    const toast = useRef(null);
    const [validationIssues, setValidationIssues] = useState({});
    const [deployDialogVisible, setDeployDialogVisible] = useState(false);
    const [deployTargets, setDeployTargets] = useState([]);
    const [deployName, setDeployName] = useState('');
    const deployNameError = validMissionName(deployName);
    const deploying = uploadState != null && !isUploadFinished(uploadState);

    // Undo / redo history
    const pastRef = useRef([]);
    const futureRef = useRef([]);
    const [canUndo, setCanUndo] = useState(false);
    const [canRedo, setCanRedo] = useState(false);
    useEffect(() => { schemaRef.current = schema; }, [schema]);
    // The schema follows the vehicle's driver cap; nodes keep their own copy
    // of their type's schema, so refresh it.
    useEffect(() => {
        setNodes(ns => ns.map(n => ({ ...n, data: { ...n.data, schema: schema.actions[n.data.type_name] } })));
    }, [schema]);
    // A mission compiled for one vehicle says nothing about another.
    useEffect(() => { setMissionCompiled(false); }, [flow.vehicle]);
    // Compile and Build use the vehicle's driver config, when one is picked.
    const vehiclePart = flow.vehicle ? { vehicle: flow.vehicle } : {};

    useEffect(() => {
        const timer = setTimeout(() => {
            const { issues } = runValidation(nodes, schema, startNodeId);
            setValidationIssues(issues);
        }, 500);
        return () => clearTimeout(timer);
    }, [nodes, startNodeId, schema]);

    const [renderedDsl, setRenderedDsl] = useState('');
    // Debounced like validation above. The backend renders the DSL from
    // the same AST it compiles, so the tab never drifts from what's built.
    useEffect(() => {
        if (nodes.length === 0) return undefined;
        const controller = new AbortController();
        const timer = setTimeout(() => {
            renderDsl(missionRequestBody(nodes, edges, eventInstances, startNodeId), controller.signal)
                .then(setRenderedDsl)
                .catch(e => {
                    if (e.name !== 'AbortError') setRenderedDsl(`# DSL preview unavailable: ${e.message}`);
                });
        }, 300);
        return () => {
            clearTimeout(timer);
            controller.abort();
        };
    }, [nodes, edges, eventInstances, startNodeId]);

    function snapshotCurrent() {
        return {
            nodes: nodes.map(n => ({
                id: n.id,
                type: n.type,
                position: n.position,
                data: {
                    type_name: n.data.type_name,
                    instance_id: n.data.instance_id,
                    params: n.data.params,
                    isStart: n.data.isStart,
                },
            })),
            edges,
            eventInstances,
            startNodeId,
        };
    }

    function applySnapshot(snap) {
        setNodes(hydrateNodes(snap.nodes));
        setEdges(snap.edges);
        setEventInstances(snap.eventInstances);
        setStartNodeId(snap.startNodeId);
    }

    function pushSnapshot() {
        const snap = snapshotCurrent();
        pastRef.current = [...pastRef.current.slice(-49), snap];
        futureRef.current = [];
        setCanUndo(true);
        setCanRedo(false);
    }

    function undo() {
        if (!pastRef.current.length) return;
        const snap = pastRef.current[pastRef.current.length - 1];
        futureRef.current = [snapshotCurrent(), ...futureRef.current.slice(0, 49)];
        pastRef.current = pastRef.current.slice(0, -1);
        applySnapshot(snap);
        setCanUndo(pastRef.current.length > 0);
        setCanRedo(true);
    }

    function redo() {
        if (!futureRef.current.length) return;
        const snap = futureRef.current[0];
        pastRef.current = [...pastRef.current.slice(-49), snapshotCurrent()];
        futureRef.current = futureRef.current.slice(1);
        applySnapshot(snap);
        setCanUndo(true);
        setCanRedo(futureRef.current.length > 0);
    }

    // Stable keyboard handler via refs so it doesn't re-register on every render
    const undoRef = useRef(undo);
    const redoRef = useRef(redo);
    useEffect(() => { undoRef.current = undo; });
    useEffect(() => { redoRef.current = redo; });
    useEffect(() => {
        function onKeyDown(e) {
            if (!(e.ctrlKey || e.metaKey)) return;
            if (e.key === 'z' && !e.shiftKey) { e.preventDefault(); undoRef.current(); }
            if (e.key === 'y' || (e.key === 'z' && e.shiftKey)) { e.preventDefault(); redoRef.current(); }
        }
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, []);

    useEffect(() => saveFlowDraft(flow), [flow]);

    // Debounced, since dragging a node changes it every frame; pagehide
    // flushes whatever the debounce hasn't written yet.
    const missionDraftRef = useRef(null);
    useEffect(() => {
        missionDraftRef.current = { ...snapshotCurrent(), features };
        const timer = setTimeout(() => saveMissionDraft(missionDraftRef.current), 500);
        return () => clearTimeout(timer);
    }, [nodes, edges, eventInstances, startNodeId, features]);
    useEffect(() => {
        const flush = () => { if (missionDraftRef.current) saveMissionDraft(missionDraftRef.current); };
        window.addEventListener('pagehide', flush);
        return () => window.removeEventListener('pagehide', flush);
    }, []);

    // An old-format draft is dropped at load; say so once after mount.
    useEffect(() => {
        if (draftNotice.current) {
            toast.current?.show({ severity: 'warn', summary: 'Flow draft not loaded', detail: draftNotice.current });
            draftNotice.current = null;
        }
    }, []);

    // Engine fields offer the flow's engines of the field's engine type.
    const engines = useCallback((engineType) => engineChoices(flow, catalog, engineType), [flow, catalog]);

    // The flow to ship with Validate and Deploy: {flow: template} when the
    // flow has nodes and is valid, {} when it has none, and null (after a
    // toast) when it's broken -- a broken flow is never sent.
    function flowRequestPart() {
        if (flow.nodes.length === 0) return {};
        if (!catalog || (catalog.daemons.length === 0 && catalog.error)) {
            toast.current.show({
                severity: 'warn', summary: 'Flow catalog unavailable',
                detail: `The flow catalog isn't loaded (${catalog?.error ?? 'not fetched yet'}); open the Flow Builder tab to retry`,
            });
            return null;
        }
        try {
            if (validateFlow(flow, catalog).errors.length > 0) throw new Error('invalid flow');
            return { flow: toTemplate(flow, catalog) };
        } catch {
            toast.current.show({ severity: 'warn', summary: 'Flow has errors', detail: 'Fix the flow on the Flow Builder tab first' });
            return null;
        }
    }

    const startNode = nodes.find(n => n.id === startNodeId);

    function buildMissionRequestBody() {
        return missionRequestBody(nodes, edges, eventInstances, startNodeId);
    }

    function highlightErrorNodes(errors) {
        const errorIds = new Set(errors.map(e => e.node_id));
        setNodes(ns => ns.map(n => ({
            ...n,
            data: { ...n.data, _hasError: errorIds.has(n.data.instance_id) },
        })));
    }

    async function handleCompile() {
        if (!startNodeId) {
            toast.current.show({ severity: 'warn', summary: 'No start state', detail: 'Right-click a node and set it as the start state.' });
            return;
        }
        const flowPart = flowRequestPart();
        if (!flowPart) return;
        setCompiling(true);
        try {
            const body = { ...buildMissionRequestBody(), ...flowPart, ...vehiclePart, geojson: features };
            const resp = await fetch(getApiUrl('/api/compile'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                let detail = `Server error ${resp.status}`;
                try {
                    const err = await resp.json();
                    const d = err.detail;
                    if (d !== undefined && d !== null) detail = typeof d === 'string' ? d : JSON.stringify(d);
                } catch { /* keep the status message */ }
                toast.current.show({ severity: 'error', summary: 'Compile failed', detail });
                return;
            }
            const result = await resp.json();
            if (result.errors) {
                toast.current.show({ severity: 'error', summary: 'Compile error', detail: result.errors[0]?.message });
                highlightErrorNodes(result.errors);
            } else {
                setMissionCompiled(true);
                setNodes(ns => ns.map(n => ({ ...n, data: { ...n.data, _hasError: false } })));
                toast.current.show({ severity: 'success', summary: 'Compiled', detail: 'Mission is valid — ready to build.' });
            }
        } catch (e) {
            toast.current.show({ severity: 'error', summary: 'Compile failed', detail: e.message });
        } finally {
            setCompiling(false);
        }
    }

    async function handleDownloadBinary(arch) {
        if (!missionCompiled) return;
        setBuilding(arch);
        try {
            const body = { ...buildMissionRequestBody(), ...vehiclePart, arch, geojson: features };
            const resp = await fetch(getApiUrl('/api/build'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                let detail = `Server error ${resp.status}`;
                try { const err = await resp.json(); detail = err.detail ?? detail; } catch { detail = await resp.text().catch(() => detail); }
                toast.current.show({ severity: 'error', summary: 'Build failed', detail });
                return;
            }
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `mission-${arch}`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            toast.current.show({ severity: 'error', summary: 'Build failed', detail: e.message });
        } finally {
            setBuilding(null);
        }
    }

    function openDeployDialog() {
        setDeployTargets(squadList ?? []);
        setDeployDialogVisible(true);
    }

    async function handleDeploy() {
        const targets = [...deployTargets];
        const flowPart = flowRequestPart();
        if (!flowPart) return;
        setDeployDialogVisible(false);
        if (deploying) {
            toast.current.show({ severity: 'warn', summary: 'Upload already in progress', detail: 'Wait for the current mission upload to finish before starting another.' });
            return;
        }
        setUploadState(initialUploadState(targets));
        const body = { ...buildMissionRequestBody(), ...flowPart, geojson: features, vehicles: targets, name: deployName };
        try {
            await streamUpload(getApiUrl('/api/deploy'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            }, (ev) => {
                setUploadState(s => applyUploadEvent(s, ev));
                if (ev.type === 'error' && ev.errors?.length) highlightErrorNodes(ev.errors);
            });
            setUploadState(s => finishUpload(s));
        } catch (e) {
            setUploadState(s => applyUploadEvent(s, { type: 'error', detail: e.message }));
        }
    }

    function updateNodeParams(nodeId, params) {
        pushSnapshot();
        setNodes(ns => ns.map(n => n.id === nodeId ? { ...n, data: { ...n.data, params } } : n));
    }

    function updateNodeId(nodeId, newId) {
        pushSnapshot();
        setNodes(ns => ns.map(n => n.id === nodeId ? { ...n, data: { ...n.data, instance_id: newId } } : n));
    }

    function updateEventParams(instanceId, params) {
        pushSnapshot();
        setEventInstances(evs => evs.map(ev => ev.instance_id === instanceId ? { ...ev, params } : ev));
    }

    function deleteEdge(edgeId) {
        pushSnapshot();
        setEdges(es => es.filter(e => e.id !== edgeId));
    }

    function deleteNodeById(nodeId) {
        pushSnapshot();
        setNodes(ns => ns.filter(n => n.id !== nodeId));
        setEdges(es => es.filter(e => e.source !== nodeId && e.target !== nodeId));
        if (startNodeId === nodeId) setStartNodeId(null);
        setPanelNodeId(null);
    }

    function handleNewMission() {
        if (nodes.length === 0 && edges.length === 0) return;
        if (!window.confirm('Start a new mission? The current canvas will be cleared.')) return;
        pushSnapshot();
        setNodes([]);
        setEdges([]);
        setEventInstances([]);
        setStartNodeId(null);
        setMissionCompiled(false);
        setDslEdited(false);
        setDslText('');
    }

    function loadFromParsed(parsed) {
        pushSnapshot();

        // Use instance_id as the React Flow node id for simplicity
        const rfNodes = parsed.nodes.map(n => ({
            id: n.instance_id,
            type: 'taskNode',
            position: { x: 0, y: 0 },
            data: {
                type_name: n.type_name,
                instance_id: n.instance_id,
                params: n.params,
                isStart: n.instance_id === parsed.start_id,
                schema: schema.actions[n.type_name],
                onOpenPanel: () => setPanelNodeId(n.instance_id),
            },
        }));

        const rfEdges = parsed.edges.map(e => {
            const isSelfLoop = e.source === e.target;
            const evId = e.event_id;
            return {
                id: `e-${e.source}-${evId}-${e.target}`,
                source: e.source,
                target: e.target,
                type: isSelfLoop ? 'selfLoop' : 'smoothstep',
                data: { eventId: evId },
                label: evId,
                ...edgeAppearance(evId),
            };
        });

        const nodeIds = rfNodes.map(n => n.id);
        const positions = bfsLayout(nodeIds, parsed.start_id, parsed.edges);
        rfNodes.forEach(n => { n.position = positions[n.id] || { x: 0, y: 0 }; });

        setNodes(rfNodes);
        setEdges(rfEdges);
        setEventInstances(parsed.events);
        setStartNodeId(parsed.start_id ?? (rfNodes[0]?.id ?? null));
        setMissionCompiled(false);
        // Canvas now reflects this parse; drop any stale frozen DSL-tab edit
        // so the tab goes back to mirroring liveDsl.
        setDslEdited(false);
        setDslText('');
    }

    function showParseWarnings(parsed) {
        (parsed.warnings ?? []).forEach(w =>
            toast.current.show({ severity: 'warn', summary: 'DSL warning', detail: w, life: 8000 }));
    }

    async function parseDslText(text) {
        const resp = await fetch(getApiUrl('/api/parse_dsl'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dsl: text }),
        });
        if (!resp.ok) {
            let detail = `Server error ${resp.status}`;
            try { const err = await resp.json(); detail = err.detail ?? detail; } catch { /* keep default */ }
            throw new Error(detail);
        }
        return resp.json();
    }

    async function handleLoadDsl(file) {
        if (!file) return;
        setLoadingDsl(true);
        try {
            const text = await file.text();
            const parsed = await parseDslText(text);
            loadFromParsed(parsed);
            showParseWarnings(parsed);
            toast.current.show({ severity: 'success', summary: 'DSL loaded', detail: `${parsed.nodes.length} actions, ${parsed.events.length} events` });
        } catch (e) {
            toast.current.show({ severity: 'error', summary: 'DSL parse error', detail: e.message });
        } finally {
            setLoadingDsl(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    }

    async function handleApplyDsl() {
        setApplyingDsl(true);
        try {
            const parsed = await parseDslText(dslText);
            loadFromParsed(parsed);
            showParseWarnings(parsed);
            toast.current.show({ severity: 'success', summary: 'DSL applied', detail: `${parsed.nodes.length} actions, ${parsed.events.length} events` });
        } catch (e) {
            toast.current.show({ severity: 'error', summary: 'DSL parse error', detail: e.message });
        } finally {
            setApplyingDsl(false);
        }
    }

    function handleDiscardDsl() {
        setDslEdited(false);
        setDslText('');
    }

    async function handleExportDsl() {
        let dsl;
        try {
            dsl = await renderDsl(buildMissionRequestBody());
        } catch (e) {
            toast.current.show({ severity: 'error', summary: 'Export failed', detail: e.message });
            return;
        }
        const blob = new Blob([dsl], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'mission.dsl';
        a.click();
        URL.revokeObjectURL(url);
    }

    const liveDsl = nodes.length > 0 ? renderedDsl : '# Add nodes to see DSL preview';
    const displayDsl = dslEdited ? dslText : liveDsl;

    const buildArchOptions = [
        { label: 'Download amd64', icon: 'pi pi-download', command: () => handleDownloadBinary('amd64') },
        { label: 'Download arm64', icon: 'pi pi-download', command: () => handleDownloadBinary('arm64') },
    ];

    const nodesWithWarnings = useMemo(
        () => nodes.map(n => ({
            ...n,
            data: { ...n.data, _warnings: validationIssues[n.id] ?? [] },
        })),
        [nodes, validationIssues]
    );

    const totalWarnings = useMemo(
        () => Object.values(validationIssues).reduce((s, arr) => s + arr.length, 0),
        [validationIssues]
    );

    return (
        <>
            <Toast ref={toast} />
            <Dialog header="Deploy mission" visible={deployDialogVisible} style={{ width: '28rem' }}
                onHide={() => setDeployDialogVisible(false)}
                footer={
                    <Button label="Deploy" icon="pi pi-send" disabled={deployTargets.length === 0 || deployNameError !== null} onClick={handleDeploy} />
                }>
                <label htmlFor="deploy-name" className="block mb-2">Mission name</label>
                <InputText id="deploy-name" className="w-full" value={deployName}
                    onChange={e => setDeployName(e.target.value)}
                    invalid={deployName !== '' && deployNameError !== null}
                    placeholder="e.g. survey_north" />
                {deployName !== '' && deployNameError && <small className="block mt-1 p-error">{deployNameError}</small>}
                <label htmlFor="deploy-targets" className="block mt-3 mb-2">Target vehicles</label>
                <MultiSelect id="deploy-targets" className="w-full" display="chip"
                    value={deployTargets}
                    options={vehicles.map(v => v.name)}
                    onChange={e => setDeployTargets(e.value)}
                    placeholder="Select vehicles" />
                <p id="deploy-flow" className="mt-3 mb-0">
                    {flow.nodes.length > 0 ? `Flow: ${flow.name} (${flow.nodes.filter(n => n.kind === 'engine').length} engines)` : 'No flow'}
                </p>
            </Dialog>
            <TabView renderActiveOnly={false} activeIndex={activeIndex} onTabChange={(e) => setActiveIndex(e.index)}>
                <TabPanel header="Mission Builder" leftIcon="pi pi-share-alt mr-2" headerClassName="mr-2">
                    <div className="flex flex-column" style={{ height: 'calc(100vh - 180px)' }}>
                        {/* Hidden file input — outside toolbar div */}
                        <input
                            ref={fileInputRef}
                            type="file"
                            accept=".dsl,.txt"
                            style={{ display: 'none' }}
                            onChange={e => handleLoadDsl(e.target.files[0])}
                        />

                        {/* Toolbar — now at top with borderBottom */}
                        <div className="flex gap-2 align-items-center p-2" style={{ borderBottom: '1px solid #2a3a4a', flexShrink: 0 }}>
                            <VehiclePicker group="mission-vehicle" flow={flow} setFlow={setFlow} catalog={catalog} />
                            <Button
                                icon="pi pi-undo"
                                size="small"
                                outlined
                                disabled={!canUndo}
                                onClick={undo}
                                tooltip="Undo (Ctrl+Z)"
                                tooltipOptions={{ position: 'top' }}
                            />
                            <Button
                                icon="pi pi-refresh"
                                size="small"
                                outlined
                                disabled={!canRedo}
                                onClick={redo}
                                tooltip="Redo (Ctrl+Shift+Z)"
                                tooltipOptions={{ position: 'top' }}
                            />
                            <Button
                                label="New"
                                icon="pi pi-file"
                                size="small"
                                outlined
                                disabled={nodes.length === 0 && edges.length === 0}
                                onClick={handleNewMission}
                                tooltip="Clear canvas and start a new mission"
                                tooltipOptions={{ position: 'top' }}
                            />
                            <Button
                                label="Load DSL"
                                icon="pi pi-folder-open"
                                size="small"
                                outlined
                                loading={loadingDsl}
                                onClick={() => fileInputRef.current?.click()}
                            />
                            <Button
                                label="Export DSL"
                                icon="pi pi-file-export"
                                size="small"
                                outlined
                                disabled={nodes.length === 0}
                                onClick={handleExportDsl}
                            />
                            <Button
                                label="Compile"
                                icon="pi pi-cog"
                                size="small"
                                loading={compiling}
                                onClick={handleCompile}
                            />
                            <SplitButton
                                label={building ? `Building ${building}…` : 'Download amd64'}
                                icon="pi pi-download"
                                size="small"
                                outlined
                                disabled={!missionCompiled}
                                loading={!!building}
                                onClick={() => handleDownloadBinary('amd64')}
                                model={buildArchOptions}
                            />
                            <Button
                                label="Deploy"
                                icon="pi pi-send"
                                size="small"
                                disabled={!missionCompiled || deploying}
                                loading={deploying}
                                onClick={openDeployDialog}
                                tooltip="Build and upload to vehicles"
                                tooltipOptions={{ position: 'top' }}
                            />
                        </div>

                        {/* Canvas — middle */}
                        <div className="flex flex-1" style={{ overflow: 'hidden', ...canvasThemeVars(theme) }}>
                            <FsmPalette vehicle={flow.vehicle} onSchemaLoaded={setSchema} />
                            <ReactFlowProvider>
                                <FsmCanvas
                                    active={visible && activeIndex === 0}
                                    nodes={nodesWithWarnings} edges={edges}
                                    setNodes={setNodes} setEdges={setEdges}
                                    eventInstances={eventInstances} setEventInstances={setEventInstances}
                                    startNodeId={startNodeId} setStartNodeId={setStartNodeId}
                                    schema={schema} features={features}
                                    panelNode={panelNode} setPanelNode={setPanelNodeId}
                                    setPanelEdgeId={setPanelEdgeId}
                                    pushSnapshot={pushSnapshot}
                                    toast={toast}
                                    theme={theme}
                                    engines={engines}
                                />
                            </ReactFlowProvider>
                        </div>

                        {/* Status bar — separate div at bottom with borderTop */}
                        <div className="flex align-items-center px-2 py-1" style={{ borderTop: '1px solid #2a3a4a', flexShrink: 0 }}>
                            <span className="text-sm ml-auto" style={{ color: '#888' }}>
                                {nodes.length} actions · {eventInstances.length} events
                                {startNode
                                    ? ` · start: ${startNode.data.instance_id}`
                                    : <span style={{ color: '#e8c87a' }}> · ⚠ no start set</span>}
                                {totalWarnings > 0 && (
                                    <span style={{ color: '#e8c87a' }}> · ⚠ {totalWarnings} warning{totalWarnings !== 1 ? 's' : ''}</span>
                                )}
                            </span>
                        </div>
                    </div>

                    <TaskNodePanel
                        visible={!!panelNode}
                        onHide={() => setPanelNodeId(null)}
                        node={panelNode}
                        schema={panelNode ? schema.actions[panelNode.data.type_name] : null}
                        namedAreas={getNamedAreas(features)}
                        engines={engines}
                        enums={schema.enums}
                        onUpdate={updateNodeParams}
                        onUpdateId={updateNodeId}
                        onDelete={deleteNodeById}
                    />

                    <EdgePanel
                        visible={!!panelEdge}
                        onHide={() => setPanelEdgeId(null)}
                        edge={panelEdge}
                        eventInstance={panelEventInstance}
                        eventSchema={panelEventInstance ? schema.events[panelEventInstance.type_name] : null}
                        engines={engines}
                        enums={schema.enums}
                        sourceLabel={panelEdgeSourceLabel}
                        targetLabel={panelEdgeTargetLabel}
                        onUpdateEvent={updateEventParams}
                        onDeleteEdge={deleteEdge}
                    />
                </TabPanel>

                <TabPanel header="Flow Builder" leftIcon="pi pi-sitemap mr-2" headerClassName="mr-2">
                    <FlowBuilder theme={theme} toast={toast} active={visible && activeIndex === 1}
                        flow={flow} setFlow={setFlow} catalog={catalog} setCatalog={setCatalog} />
                </TabPanel>
                <TabPanel header="Map" leftIcon="pi pi-map mr-2" headerClassName="mr-2">
                    <div style={{ height: 'calc(100vh - 180px)' }}>
                        <MapDraw features={features} setFeatures={setFeatures} toast={toast} />
                    </div>
                </TabPanel>

                <TabPanel header="DSL" leftIcon="pi pi-code mr-2" headerClassName="mr-2">
                    <div className="flex flex-column" style={{ height: 'calc(100vh - 180px)' }}>
                        <div className="flex gap-2 align-items-center p-2" style={{ borderBottom: '1px solid #2a3a4a', flexShrink: 0 }}>
                            <Button
                                label="Export DSL"
                                icon="pi pi-file-export"
                                size="small"
                                outlined
                                disabled={nodes.length === 0}
                                onClick={handleExportDsl}
                            />
                            {dslEdited && (
                                <>
                                    <Button
                                        label="Apply"
                                        icon="pi pi-check"
                                        size="small"
                                        severity="success"
                                        loading={applyingDsl}
                                        onClick={handleApplyDsl}
                                    />
                                    <Button
                                        label="Discard"
                                        icon="pi pi-times"
                                        size="small"
                                        outlined
                                        disabled={applyingDsl}
                                        onClick={handleDiscardDsl}
                                    />
                                    <span className="text-sm" style={{ color: '#e8c87a' }}>
                                        ⚠ unapplied edits — canvas changes won't appear here until Apply/Discard
                                    </span>
                                </>
                            )}
                        </div>
                        <InputTextarea
                            style={{ flex: 1, width: '100%', fontFamily: 'monospace', fontSize: 12, resize: 'none' }}
                            value={displayDsl}
                            onChange={e => { setDslEdited(true); setDslText(e.target.value); }}
                        />
                    </div>
                </TabPanel>
            </TabView>
        </>
    );
}

export default PlanPage;
