import { describe, it, expect } from 'vitest';
import { formToState, stateToForm } from './configForm.js';
import { newActor, newWaypoint, orphanService, parsePoint, prefillPlugin } from './simForm.js';

const SIM_PKG = 'https://github.com/cmusatyalab/steeleagle_plugins//simulator@v4_backend_rework';
const packages = [{ uri: SIM_PKG, plugins: [{ name: 'sim_detector', type: 'engine', args: [] }, { name: 'sim_vehicle', type: 'driver', args: [] }] }];

const service = (name, type, simulate = true) => ({
    name, type, auth: '', simulate,
    plugin: { plugin: type === 'vehicle' ? 'sim_vehicle' : 'sim_detector', package: SIM_PKG, isolation: '', args: null, extra: null },
    extra: null,
});

// Mihir's config: one simulated vehicle and engine, and an actor
function simForm(args, services = [service('steppeeagle', 'vehicle'), service('yolo_sim', 'engine')]) {
    return {
        daemon: { port: null, lcs: null, hostname: null, cert: null, orchestrator: null, extra: null },
        simulator: { package: SIM_PKG, args, extra: null },
        services,
        extra: null,
    };
}

const mihir = () => ({
    vehicles: [{ name: 'steppeeagle', interface: 'steeleagle', origin: [40.33862, -79.86521, 0] }],
    engines: [{ name: 'yolo_sim', interface: 'detection' }],
    actors: [{ tag: 'person', origin: [40.33862, -79.86521, 0] }],
});

const roundTrip = (form) => stateToForm(formToState(form, packages));

describe('parsePoint', () => {
    it('reads arrays and tables', () => {
        expect(parsePoint([1, 2])).toEqual({ lat: 1, lon: 2, alt: null, extra: null });
        expect(parsePoint({ lat: 1, lon: 2, alt: 3, speed: 4 })).toEqual({ lat: 1, lon: 2, alt: 3, extra: { speed: 4 } });
        expect(parsePoint([1, 2, 3, 4])).toBeNull();
        expect(parsePoint(['a', 2])).toBeNull();
    });
});

describe('simulator entries', () => {
    it('links each simulated service to its entry and writes the config back unchanged', () => {
        const st = formToState(simForm(mihir()), packages);
        expect(st.services[0].sim).toMatchObject({ interface: 'steeleagle', origin: { lat: 40.33862, lon: -79.86521, alt: 0 } });
        expect(st.services[1].sim.interface).toBe('detection');
        expect(st.simulator.actors).toHaveLength(1);
        expect(st.simulator.orphans).toEqual([]);
        expect(st.simulator.argRows).toEqual([]);
        const { form, errors } = stateToForm(st);
        expect(errors).toEqual({});
        expect(form.simulator.args).toEqual(mihir());
    });

    it('follows a renamed service', () => {
        const st = formToState(simForm(mihir()), packages);
        st.services[0].name = 'eagle2';
        expect(stateToForm(st).form.simulator.args.vehicles[0].name).toBe('eagle2');
    });

    it('writes a newly simulated vehicle, and drops an unsimulated one', () => {
        const st = formToState(simForm({}, [service('a', 'vehicle', false), service('b', 'vehicle')]), packages);
        st.services[0].simulate = true;
        st.services[0].sim.origin = { lat: 1, lon: 2, alt: null };
        st.services[1].simulate = false;
        const { form, errors } = stateToForm(st);
        expect(errors).toEqual({});
        expect(form.simulator.args.vehicles).toEqual([{ name: 'a', origin: [1, 2] }]);
    });

    it('needs a simulated vehicle to have an origin', () => {
        const st = formToState(simForm({}, [service('a', 'vehicle')]), packages);
        expect(stateToForm(st).errors[`services.${st.services[0].key}.sim`]).toEqual({ origin: 'Latitude and longitude are required' });
    });

    it('refuses simulated extras and simulated services without a simulator', () => {
        const st = formToState(simForm({}, [service('x', 'extra')]), packages);
        expect(stateToForm(st).errors[`services.${st.services[0].key}.simulate`]).toMatch(/Only vehicles and engines/);
        st.services[0].type = 'vehicle';
        st.simulator.present = false;
        expect(stateToForm(st).errors[`services.${st.services[0].key}.simulate`]).toMatch(/Needs a simulator/);
    });

    it('keeps entries no service claims as orphans', () => {
        const args = { ...mihir(), engines: [{ name: 'yolo_sim' }, { name: 'stray', range: 20 }] };
        const st = formToState(simForm(args), packages);
        expect(st.simulator.orphans).toMatchObject([{ kind: 'engine', entry: { name: 'stray', range: 20 } }]);
        expect(stateToForm(st).form.simulator.args.engines).toEqual([{ name: 'yolo_sim' }, { name: 'stray', range: 20 }]);
    });

    it('turns an orphan into a simulated service', () => {
        const st = formToState(simForm({ vehicles: [{ name: 'lost', origin: [1, 2, 3] }] }, []), packages);
        const base = { key: 'k', origName: null, name: '', type: 'engine', auth: '', simulate: false, plugin: '', package: '', isolation: '', args: { declared: [], rows: [] }, hadArgs: false, extra: null, pluginExtra: null };
        const svc = orphanService(st.simulator.orphans[0], base, SIM_PKG, packages);
        expect(svc).toMatchObject({ name: 'lost', type: 'vehicle', simulate: true, plugin: 'sim_vehicle', package: SIM_PKG });
        st.services.push(svc);
        st.simulator.orphans = [];
        expect(stateToForm(st).form.simulator.args.vehicles).toEqual([{ name: 'lost', origin: [1, 2, 3] }]);
    });

    it('prefills a plugin only when the service has none', () => {
        const svc = { type: 'engine', plugin: '', package: '' };
        expect(prefillPlugin(svc, SIM_PKG, packages)).toMatchObject({ plugin: 'sim_detector', package: SIM_PKG });
        expect(prefillPlugin({ ...svc, plugin: 'mine' }, SIM_PKG, packages).package).toBe('');
    });

    it('keeps an origin it cannot show, and lat/lon keys become an origin', () => {
        const args = { vehicles: [{ name: 'steppeeagle', origin: { lat: 1, lon: 2 } }], engines: [{ name: 'yolo_sim' }] };
        expect(roundTrip(simForm(args)).form.simulator.args.vehicles).toEqual(args.vehicles);
        const flat = { vehicles: [{ name: 'steppeeagle', lat: 1, lon: 2, alt: 3 }] };
        expect(roundTrip(simForm(flat)).form.simulator.args.vehicles).toEqual([{ name: 'steppeeagle', origin: [1, 2, 3] }]);
    });

    it('keeps the other simulator args as rows, and refuses a row named like a list', () => {
        const st = formToState(simForm({ ...mihir(), seed: 7 }), packages);
        expect(st.simulator.argRows.map((r) => r.key)).toEqual(['seed']);
        st.simulator.argRows[0].key = 'actors';
        expect(stateToForm(st).errors['simulator.args']).toEqual({ [st.simulator.argRows[0].id]: '"actors" is edited above' });
    });
});

describe('actors', () => {
    it('round-trips waypoints, as arrays or with speed and delay', () => {
        const actors = [{ tag: 'car', name: 'c1', origin: [1, 2], waypoints: [[1, 2.5, 0], { lat: 1, lon: 3, speed: 5, delay: 2 }], args: { length: 4, waypoint_cycle: true } }];
        const { form, errors } = roundTrip(simForm({ ...mihir(), actors }));
        expect(errors).toEqual({});
        expect(form.simulator.args.actors).toEqual(actors);
    });

    it('validates a new actor and its waypoints', () => {
        const st = formToState(simForm(mihir()), packages);
        const actor = { ...newActor(), waypoints: [newWaypoint()] };
        st.simulator.actors.push(actor);
        expect(stateToForm(st).errors['simulator.actors']).toEqual({
            [actor.key]: { tag: 'Required', origin: 'Latitude and longitude are required', waypoints: { [actor.waypoints[0].key]: 'Latitude and longitude are required' } },
        });
    });

    it('keeps an empty list it had', () => {
        const st = formToState(simForm({ ...mihir(), actors: [] }), packages);
        expect(stateToForm(st).form.simulator.args.actors).toEqual([]);
    });
});
