package mission

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/rs/zerolog"
)

// Lister lists the running vehicles.
type Lister func(ctx context.Context) ([]fleet.Vehicle, error)

// VehicleSender uploads a mission to one vehicle and loads it; *Sender is
// one. An error's text is the vehicle's result.
type VehicleSender interface {
	Send(ctx context.Context, vehicle, name string, binary []byte, progress Progress) (string, error)
}

// Uploads serves the mission upload and deploy routes, which stream NDJSON.
type Uploads struct {
	list    Lister
	send    VehicleSender
	svc     *Service
	log     zerolog.Logger
	maxSize int
}

// NewUploads returns Uploads that resolve vehicles with list, send missions
// with send, and compile deployed graphs with svc.
func NewUploads(list Lister, send VehicleSender, svc *Service, log zerolog.Logger) *Uploads {
	return &Uploads{list: list, send: send, svc: svc, log: log, maxSize: MaxMissionSize}
}

// stream writes NDJSON events, one flushed line each, safe for concurrent use.
type stream struct {
	mu sync.Mutex
	w  http.ResponseWriter
	f  http.Flusher
}

func newStream(w http.ResponseWriter) *stream {
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	f, _ := w.(http.Flusher)
	return &stream{w: w, f: f}
}

func (s *stream) emit(ev map[string]any) {
	line, err := json.Marshal(ev)
	if err != nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.w.Write(append(line, '\n'))
	if s.f != nil {
		s.f.Flush()
	}
}

func (s *stream) stage(name string) { s.emit(map[string]any{"type": "stage", "stage": name}) }

func (s *stream) result(vehicle string, err error, details string) {
	if err != nil {
		details = err.Error()
	}
	s.emit(map[string]any{"type": "result", "vehicle": vehicle, "success": err == nil, "details": details})
}

func dedupe(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, v := range in {
		if !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	return out
}

// listVehicles lists the fleet by name, or writes a 503 and returns nil.
func (u *Uploads) listVehicles(w http.ResponseWriter, r *http.Request) map[string]fleet.Vehicle {
	vs, err := u.list(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "Cannot list vehicles: "+err.Error())
		return nil
	}
	by := make(map[string]fleet.Vehicle, len(vs))
	for _, v := range vs {
		by[v.Name] = v
	}
	return by
}

// resolve finds a vehicle that can be sent a mission, or says why not.
func resolve(by map[string]fleet.Vehicle, name string) (fleet.Vehicle, error) {
	v, ok := by[name]
	if !ok {
		return v, fmt.Errorf("not found")
	}
	if v.Arch == "" {
		ip, _ := fleet.SplitName(v.Name)
		return v, fmt.Errorf("daemon %s didn't report its architecture", ip)
	}
	return v, nil
}

// job is a vehicle and the binary to send it.
type job struct {
	vehicle string
	binary  []byte
}

// sendAll sends every job concurrently, streaming progress and results, and
// returns when all have finished. ctx cancels the sends that are left.
func (u *Uploads) sendAll(ctx context.Context, st *stream, name string, jobs []job) {
	st.stage("uploading")
	var wg sync.WaitGroup
	for _, j := range jobs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			details, err := u.send.Send(ctx, j.vehicle, name, j.binary, func(sent, total int) {
				st.emit(map[string]any{"type": "progress", "vehicle": j.vehicle, "sent": sent, "total": total})
			})
			if err != nil {
				u.log.Warn().Str("vehicle", j.vehicle).Err(err).Msg("mission send failed")
			}
			st.result(j.vehicle, err, details)
		}()
	}
	wg.Wait()
}

// ServeUpload serves POST /api/upload: multipart "files" (at most two
// binaries, one per architecture), "vehicles" and "name".
func (u *Uploads) ServeUpload(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseMultipartForm(2*int64(u.maxSize) + 1<<20); err != nil {
		writeError(w, http.StatusBadRequest, "invalid upload: "+err.Error())
		return
	}
	defer r.MultipartForm.RemoveAll()
	vehicles := dedupe(r.MultipartForm.Value["vehicles"])
	if len(vehicles) == 0 {
		writeError(w, http.StatusBadRequest, "Select at least one vehicle")
		return
	}
	name := r.FormValue("name")
	if err := ValidName(name); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	headers := r.MultipartForm.File["files"]
	if len(headers) > 2 {
		writeError(w, http.StatusBadRequest, "At most two mission binaries (one per architecture) can be uploaded at once")
		return
	}
	if len(headers) == 0 {
		writeError(w, http.StatusBadRequest, "Select at least one mission binary")
		return
	}
	variants := map[string][]byte{}
	for _, h := range headers {
		f, err := h.Open()
		if err != nil {
			writeError(w, http.StatusBadRequest, h.Filename+": "+err.Error())
			return
		}
		data, err := io.ReadAll(io.LimitReader(f, int64(u.maxSize)+1))
		f.Close()
		if err != nil {
			writeError(w, http.StatusBadRequest, h.Filename+": "+err.Error())
			return
		}
		if len(data) > u.maxSize {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("%s is larger than the %d MiB limit", h.Filename, u.maxSize>>20))
			return
		}
		arch, err := ElfArch(data)
		if err != nil {
			writeError(w, http.StatusBadRequest, h.Filename+": "+err.Error())
			return
		}
		if _, dup := variants[arch]; dup {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("Two files were built for %s; upload at most one per architecture", arch))
			return
		}
		variants[arch] = data
	}
	by := u.listVehicles(w, r)
	if by == nil {
		return
	}

	st := newStream(w)
	var jobs []job
	for _, n := range vehicles {
		v, err := resolve(by, n)
		if err == nil {
			if bin, ok := variants[v.Arch]; ok {
				jobs = append(jobs, job{n, bin})
				continue
			}
			err = fmt.Errorf("no binary for %s", v.Arch)
		}
		st.result(n, err, "")
	}
	if len(jobs) > 0 {
		u.sendAll(r.Context(), st, name, jobs)
	}
}

// deployRequest is a mission graph with the vehicles to deploy it to.
type deployRequest struct {
	Graph
	Vehicles []string `json:"vehicles"`
	Name     string   `json:"name"`
}

// ServeDeploy serves POST /api/deploy: it builds the graph for the
// architecture and driver config of each vehicle, then uploads. If any build
// fails nothing is uploaded.
func (u *Uploads) ServeDeploy(w http.ResponseWriter, r *http.Request) {
	var req deployRequest
	if !decodeBody(w, r, &req) {
		return
	}
	vehicles := dedupe(req.Vehicles)
	if len(vehicles) == 0 {
		writeError(w, http.StatusBadRequest, "Select at least one vehicle")
		return
	}
	if err := ValidName(req.Name); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if _, err := u.svc.Ready(r.Context()); err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	by := u.listVehicles(w, r)
	if by == nil {
		return
	}

	st := newStream(w)
	// Step 1: structural and GeoJSON checks, once, before any vehicle is
	// resolved or anything is built.
	errs := StructuralErrors(req.Graph)
	if _, featureErrs := PrepareGeoJSON(req.Graph.GeoJSON); len(featureErrs) > 0 {
		for _, m := range featureErrs {
			errs = append(errs, GraphError{Message: m})
		}
	}
	if len(errs) > 0 {
		msgs := make([]string, len(errs))
		for i, e := range errs {
			msgs[i] = e.Message
		}
		st.emit(map[string]any{"type": "error", "detail": "Build failed: " + strings.Join(msgs, "; "), "errors": errs})
		return
	}
	type group struct {
		target   BuildTarget
		vehicles []string
	}
	var groups []*group
	index := map[string]*group{}
	for _, n := range vehicles {
		v, err := resolve(by, n)
		if err == nil && v.DriverErr != nil {
			err = fmt.Errorf("driver config: %w", v.DriverErr)
		}
		if err != nil {
			st.result(n, err, "")
			continue
		}
		key := fmt.Sprintf("%s/%x", v.Arch, sha256.Sum256(v.Config))
		g := index[key]
		if g == nil {
			g = &group{target: BuildTarget{Arch: v.Arch, Plugin: v.Config}}
			index[key] = g
			groups = append(groups, g)
		}
		g.vehicles = append(g.vehicles, n)
	}
	if len(groups) == 0 {
		return
	}

	st.stage("building")
	targets := make([]BuildTarget, len(groups))
	for i, g := range groups {
		g.target.Key = strings.Join(g.vehicles, ", ")
		targets[i] = g.target
	}
	bins, errs, err := u.svc.CompileGraph(r.Context(), req.Graph, targets)
	if len(errs) > 0 {
		msgs := make([]string, len(errs))
		for i, e := range errs {
			msgs[i] = e.Message
		}
		st.emit(map[string]any{"type": "error", "detail": "Build failed: " + strings.Join(msgs, "; "), "errors": errs})
		return
	}
	if err != nil {
		st.emit(map[string]any{"type": "error", "detail": "Build failed: " + err.Error()})
		return
	}
	var jobs []job
	for _, g := range groups {
		for _, n := range g.vehicles {
			jobs = append(jobs, job{n, bins[g.target.Key]})
		}
	}
	u.sendAll(r.Context(), st, req.Name, jobs)
}
