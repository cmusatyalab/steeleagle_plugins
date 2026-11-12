package pins

import (
	"errors"
	"slices"
	"sort"
	"strconv"
	"sync"
	"time"
)

const (
	// window is how recent a candidate's sightings must be to count towards
	// MinSightings; a candidate not seen for this long is dropped.
	window = 5 * time.Second
	// MaxGroups bounds the pins and candidates held, as a safety net.
	MaxGroups = 500
)

// Settings are how detections are grouped into pins. They are shared by
// every viewer of the GCS.
type Settings struct {
	// RadiusM is how near, in metres along the ground, a detection must be
	// to a pin of its engine and class to join it.
	RadiusM int `json:"radius_m"`
	// ExpiryS is how long, in seconds, a pin lasts after its last sighting.
	ExpiryS int `json:"expiry_s"`
	// MinSightings is how many frames must see a candidate within 5 s
	// before it is shown as a pin.
	MinSightings int `json:"min_sightings"`
}

// DefaultSettings are the settings the GCS starts with.
var DefaultSettings = Settings{RadiusM: 5, ExpiryS: 60, MinSightings: 3}

// Validate reports whether the settings are within the ranges the UI offers.
func (s Settings) Validate() error {
	switch {
	case s.RadiusM < 1 || s.RadiusM > 20:
		return errors.New("radius_m must be 1 to 20")
	case s.ExpiryS < 60 || s.ExpiryS > 3600 || s.ExpiryS%60 != 0:
		return errors.New("expiry_s must be whole minutes from 60 to 3600")
	case s.MinSightings < 1 || s.MinSightings > 5:
		return errors.New("min_sightings must be 1 to 5")
	}
	return nil
}

// Hit is one detected box placed on the ground.
type Hit struct {
	Engine, Class, Vehicle string
	// Producer and Seq identify the frame the box was found in.
	Producer string
	Seq      uint64
	Lat, Lon float64
	// At is when the frame was taken, as near as the GCS can tell.
	At time.Time
}

// Pin is an object shown on the map, as the UI reads it.
type Pin struct {
	ID     string  `json:"id"`
	Engine string  `json:"engine"`
	Class  string  `json:"class"`
	Lat    float64 `json:"lat"`
	Long   float64 `json:"long"`
	// Sightings is how many frames saw it.
	Sightings int `json:"sightings"`
	// FirstSeenS and LastSeenS are how many seconds ago it was first and
	// last seen.
	FirstSeenS float64 `json:"first_seen_s"`
	LastSeenS  float64 `json:"last_seen_s"`
	// Vehicles are the vehicles that saw it, sorted.
	Vehicles []string `json:"vehicles"`
}

type frameKey struct {
	producer string
	seq      uint64
}

// group is a pin, or a candidate for one not yet seen often enough.
type group struct {
	id            uint64
	engine, class string
	sumLat        float64
	sumLon        float64
	boxes         int
	sightings     int
	lastFrame     frameKey
	recent        []time.Time
	first, last   time.Time
	vehicles      map[string]bool
	visible       bool
}

func (g *group) lat() float64 { return g.sumLat / float64(g.boxes) }
func (g *group) lon() float64 { return g.sumLon / float64(g.boxes) }

// Store groups hits into pins. It is safe for concurrent use.
type Store struct {
	mu       sync.Mutex
	settings Settings
	groups   []*group
	nextID   uint64
}

// NewStore returns an empty store with the default settings.
func NewStore() *Store {
	return &Store{settings: DefaultSettings}
}

// Add adds a hit: it joins the nearest pin or candidate of its engine and
// class within the radius, or starts a new candidate.
func (s *Store) Add(h Hit) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.prune(h.At)
	var best *group
	bestDist := float64(s.settings.RadiusM)
	for _, g := range s.groups {
		if g.engine != h.Engine || g.class != h.Class {
			continue
		}
		if d := groundDistance(g.lat(), g.lon(), h.Lat, h.Lon); d <= bestDist {
			best, bestDist = g, d
		}
	}
	if best == nil {
		s.nextID++
		best = &group{id: s.nextID, engine: h.Engine, class: h.Class, first: h.At, last: h.At, vehicles: map[string]bool{}}
		s.groups = append(s.groups, best)
		s.evict()
	}
	best.sumLat += h.Lat
	best.sumLon += h.Lon
	best.boxes++
	best.vehicles[h.Vehicle] = true
	if h.At.After(best.last) {
		best.last = h.At
	}
	key := frameKey{h.Producer, h.Seq}
	if best.sightings > 0 && key == best.lastFrame {
		return
	}
	best.lastFrame = key
	best.sightings++
	if best.visible {
		return
	}
	best.recent = append(best.recent, h.At)
	best.recent = slices.DeleteFunc(best.recent, func(t time.Time) bool { return h.At.Sub(t) > window })
	if len(best.recent) >= s.settings.MinSightings {
		best.visible, best.recent = true, nil
	}
}

// prune drops the candidates not seen within the window and the pins past
// their expiry, as of now. s.mu is held.
func (s *Store) prune(now time.Time) {
	expiry := time.Duration(s.settings.ExpiryS) * time.Second
	s.groups = slices.DeleteFunc(s.groups, func(g *group) bool {
		if g.visible {
			return now.Sub(g.last) > expiry
		}
		return now.Sub(g.last) > window
	})
}

// evict drops the least recently seen candidates, then pins, while there are
// more than MaxGroups. s.mu is held.
func (s *Store) evict() {
	for len(s.groups) > MaxGroups {
		victim := -1
		for i, g := range s.groups {
			if victim < 0 || less(g, s.groups[victim]) {
				victim = i
			}
		}
		s.groups = slices.Delete(s.groups, victim, victim+1)
	}
}

// less is whether a should be evicted before b: candidates before pins, then
// the least recently seen.
func less(a, b *group) bool {
	if a.visible != b.visible {
		return !a.visible
	}
	return a.last.Before(b.last)
}

// Snapshot returns the pins as of now, oldest first, after dropping those
// that have expired.
func (s *Store) Snapshot(now time.Time) []Pin {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.prune(now)
	pins := []Pin{}
	for _, g := range s.groups {
		if !g.visible {
			continue
		}
		vehicles := make([]string, 0, len(g.vehicles))
		for v := range g.vehicles {
			vehicles = append(vehicles, v)
		}
		sort.Strings(vehicles)
		pins = append(pins, Pin{
			ID: strconv.FormatUint(g.id, 10), Engine: g.engine, Class: g.class,
			Lat: g.lat(), Long: g.lon(), Sightings: g.sightings,
			FirstSeenS: now.Sub(g.first).Seconds(), LastSeenS: now.Sub(g.last).Seconds(),
			Vehicles: vehicles,
		})
	}
	return pins
}

// Settings returns the current settings.
func (s *Store) Settings() Settings {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.settings
}

// SetSettings replaces the settings, unless v is invalid. A new expiry applies
// to the existing pins too; a new radius or minimum only to new hits.
func (s *Store) SetSettings(v Settings) error {
	if err := v.Validate(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.settings = v
	return nil
}

// Clear removes every pin and candidate, and returns how many pins there
// were.
func (s *Store) Clear() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, g := range s.groups {
		if g.visible {
			n++
		}
	}
	s.groups = nil
	return n
}

// groupCount is how many pins and candidates are held.
func (s *Store) groupCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.groups)
}
