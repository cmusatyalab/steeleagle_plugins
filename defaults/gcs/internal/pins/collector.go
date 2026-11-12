package pins

import (
	"context"
	"strings"
	"sync"
	"time"

	resultpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/result"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/encoding/protojson"
)

// defaultRefresh is how often the collector re-reads the vehicles and
// engines.
const defaultRefresh = 5 * time.Second

// Source lists the running vehicles and the flow catalog.
type Source func(ctx context.Context) ([]fleet.Vehicle, flows.Catalog, error)

// SubscribeFunc reads the results of the engine name, handing each to
// handle, until ctx is done.
type SubscribeFunc func(ctx context.Context, engine string, handle func(feed.Result))

// Collector places the detections of the engines on the GCS's machine on the
// ground and adds them to a store.
type Collector struct {
	store   *Store
	poses   *PoseHistory
	log     zerolog.Logger
	refresh time.Duration

	mu sync.Mutex
	// vehicles are the running vehicles by name.
	vehicles map[string]fleet.Vehicle
	// dropped are the producers and reasons already logged.
	dropped map[string]bool
}

// NewCollector returns a collector adding to store, placing detections by
// the poses in poses.
func NewCollector(store *Store, poses *PoseHistory, log zerolog.Logger) *Collector {
	return &Collector{
		store: store, poses: poses, log: log.With().Str("component", "pins").Logger(),
		refresh: defaultRefresh, vehicles: map[string]fleet.Vehicle{}, dropped: map[string]bool{},
	}
}

// SetVehicles sets the running vehicles, whose drivers say which cameras are
// on the gimbal.
func (c *Collector) SetVehicles(vs []fleet.Vehicle) {
	m := make(map[string]fleet.Vehicle, len(vs))
	for _, v := range vs {
		m[v.Name] = v
	}
	c.mu.Lock()
	c.vehicles = m
	c.mu.Unlock()
}

// Handle places the boxes of a detection result on the ground and adds them
// to the store. Other results, results from a camera not on its vehicle's
// gimbal, and results without a known pose are dropped.
func (c *Collector) Handle(r feed.Result) {
	if r.Type != "DetectionResult" {
		return
	}
	cam, ok := c.camera(r.Vehicle, r.Producer)
	if !ok {
		c.drop(r.Producer, "camera not on the gimbal or unknown")
		return
	}
	pose, ok := c.poses.Lookup(r.Producer, r.Vehicle, r.Session, r.Seq, r.OfferedAt)
	if !ok {
		c.drop(r.Producer, "no pose")
		return
	}
	var det resultpb.DetectionResult
	if err := protojson.Unmarshal(r.JSON, &det); err != nil {
		c.drop(r.Producer, "undecodable result")
		return
	}
	fov := FOV{H: float64(cam.HFov), V: float64(cam.VFov)}
	for _, d := range det.GetDetections() {
		b := d.GetBbox()
		if b.GetXMax() <= b.GetXMin() || b.GetYMax() <= b.GetYMin() {
			continue // no box, which would place it at the frame's corner
		}
		lat, lon, ok := Project(pose, fov, (b.GetXMin()+b.GetXMax())/2, (b.GetYMin()+b.GetYMax())/2)
		if !ok {
			continue
		}
		c.store.Add(Hit{
			Engine: r.Engine, Class: d.GetClassName(), Vehicle: r.Vehicle,
			Producer: r.Producer, Seq: r.Seq, Lat: lat, Lon: lon, At: r.OfferedAt,
		})
	}
}

// camera is the driver camera of producer ("<vehicle>.<camera>") on
// vehicle, if it is on the gimbal.
func (c *Collector) camera(vehicle, producer string) (config.DriverCamera, bool) {
	name := producer[strings.LastIndexByte(producer, '.')+1:]
	c.mu.Lock()
	v, ok := c.vehicles[vehicle]
	c.mu.Unlock()
	if !ok || v.Driver == nil {
		return config.DriverCamera{}, false
	}
	for _, cam := range v.Driver.Cameras {
		if cam.Name == name {
			return cam, cam.Gimbal
		}
	}
	return config.DriverCamera{}, false
}

// drop logs, once per producer and reason, why a result was dropped.
func (c *Collector) drop(producer, reason string) {
	key := producer + "\x00" + reason
	c.mu.Lock()
	logged := c.dropped[key]
	c.dropped[key] = true
	c.mu.Unlock()
	if !logged {
		c.log.Debug().Str("producer", producer).Str("reason", reason).Msg("dropping detections")
	}
}

// Run reads the vehicles and engines from src now and every refresh, and
// keeps a subscription to each detection engine on the GCS's own daemon,
// until ctx is done.
func (c *Collector) Run(ctx context.Context, src Source, subscribe SubscribeFunc) {
	subs := map[string]context.CancelFunc{}
	ticker := time.NewTicker(c.refresh)
	defer ticker.Stop()
	for {
		vehicles, catalog, err := src(ctx)
		if err != nil {
			c.log.Debug().Err(err).Msg("could not list vehicles and engines")
		} else {
			c.SetVehicles(vehicles)
			want := detectionEngines(catalog)
			for name, cancel := range subs {
				if !want[name] {
					cancel()
					delete(subs, name)
				}
			}
			for name := range want {
				if _, ok := subs[name]; ok {
					continue
				}
				subCtx, cancel := context.WithCancel(ctx)
				subs[name] = cancel
				c.log.Info().Str("engine", name).Msg("collecting detections")
				go subscribe(subCtx, name, c.Handle)
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// detectionEngines are the full names of the engines on the GCS's own daemon
// whose result is a DetectionResult.
func detectionEngines(cat flows.Catalog) map[string]bool {
	out := map[string]bool{}
	for _, d := range cat.Daemons {
		if !d.Local {
			continue
		}
		for _, e := range d.Engines {
			t := e.Result
			if t == "" {
				for _, ev := range e.Events {
					if ev.Label == "result" && ev.Type != nil {
						t = *ev.Type
					}
				}
			}
			if name, _ := feed.ResultType(t); name == "DetectionResult" {
				out[e.Name] = true
			}
		}
	}
	return out
}
