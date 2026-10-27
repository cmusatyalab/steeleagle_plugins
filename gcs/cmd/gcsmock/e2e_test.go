//go:build linux

package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// These tests run the mock and the real GCS as processes. They need liburiel
// and ffmpeg (run them under Pixi) and a built bin/gcs and web/dist (pixi run
// build). They don't run in parallel: each instance has its own Uriel names and
// port, but they are heavy.

const (
	// pollPeriod is how often every wait polls.
	pollPeriod = 200 * time.Millisecond
	// mockStopTimeout is how long the mock may take to stop after SIGINT.
	mockStopTimeout = 15 * time.Second
)

func TestMain(m *testing.M) {
	code := m.Run()
	if mockBinary.path != "" {
		os.RemoveAll(filepath.Dir(mockBinary.path))
	}
	os.Exit(code)
}

// mockBinary is the gcsmock binary, built once for all the tests.
var mockBinary struct {
	once sync.Once
	path string
	err  error
}

// instance counts the mocks this process starts, so each gets its own IPs.
var instance atomic.Int32

// mockRun is a running gcsmock.
type mockRun struct {
	t   *testing.T
	url string
	// gcsIP is the IP of the daemon the GCS runs on, and remoteIP another
	// daemon's.
	gcsIP, remoteIP string
	// alpha and bravo are on the GCS's daemon, charlie on the other.
	alpha, bravo, charlie string
}

// startMock builds the mock if needed and starts it with the vehicles alpha
// and bravo on the GCS's daemon and charlie on another, then waits for the
// GCS to serve. noEncoderRemote leaves the other daemon
// without an encoder. The mock is stopped when the test ends.
func startMock(t *testing.T, noEncoderRemote bool) *mockRun {
	t.Helper()
	gcs, err := filepath.Abs("../../bin/gcs")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(gcs); err != nil {
		t.Skipf("run pixi run build first (%v)", err)
	}
	web, err := filepath.Abs("../../web/dist")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(web, "index.html")); err != nil {
		t.Skipf("run pixi run build first (%v)", err)
	}

	mockBinary.once.Do(func() {
		dir, err := os.MkdirTemp("", "gcsmock-e2e-")
		if err != nil {
			mockBinary.err = err
			return
		}
		mockBinary.path = filepath.Join(dir, "gcsmock")
		out, err := exec.Command("go", "build", "-o", mockBinary.path, ".").CombinedOutput()
		if err != nil {
			mockBinary.err = fmt.Errorf("go build: %w\n%s", err, out)
		}
	})
	if mockBinary.err != nil {
		t.Fatal(mockBinary.err)
	}

	// Names the GCS sees are global in iceoryx's shared memory, so each
	// instance gets its own daemon IPs: a block per process and test
	n := int(instance.Add(1))
	block := 64 + os.Getpid()%64
	r := &mockRun{
		t:        t,
		gcsIP:    fmt.Sprintf("100.%d.%d.1", block, 2*n),
		remoteIP: fmt.Sprintf("100.%d.%d.1", block, 2*n+1),
	}
	r.alpha, r.bravo, r.charlie = r.gcsIP+".alpha", r.gcsIP+".bravo", r.remoteIP+".charlie"
	port := freePort(t)
	r.url = "http://127.0.0.1:" + strconv.Itoa(port)

	args := []string{
		"-gcs", gcs,
		"-web", web,
		"-http", "127.0.0.1:" + strconv.Itoa(port),
		"-socket", filepath.Join(t.TempDir(), "mock.sock"),
		// Clips are generated per test (about 0.2 s each), since their names
		// carry the per-test IPs
		"-clips", t.TempDir(),
		"-gcs-ip", r.gcsIP,
		"-vehicles", strings.Join([]string{r.alpha, r.bravo, r.charlie}, ","),
	}
	if noEncoderRemote {
		args = append(args, "-no-encoder-daemons", r.remoteIP)
	}
	cmd := exec.Command(mockBinary.path, args...)
	var output lockedBuffer
	cmd.Stdout, cmd.Stderr = &output, &output
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	var children []int

	t.Cleanup(func() {
		// The GCS may have started after the last look
		children = append(children, childrenOf(cmd.Process.Pid)...)
		_ = cmd.Process.Signal(os.Interrupt)
		var waitErr error
		select {
		case waitErr = <-exited:
		case <-time.After(mockStopTimeout):
			t.Errorf("the mock didn't stop within %v of SIGINT; killing it", mockStopTimeout)
			_ = cmd.Process.Kill()
			waitErr = <-exited
		}
		if waitErr != nil {
			t.Errorf("the mock exited with %v", waitErr)
		}
		// Its children are gone too
		deadline := time.Now().Add(5 * time.Second)
		for _, pid := range children {
			for alive(pid) && time.Now().Before(deadline) {
				time.Sleep(pollPeriod)
			}
			if alive(pid) {
				t.Errorf("the mock's child %d survived it; killing it", pid)
				_ = syscall.Kill(pid, syscall.SIGKILL)
			}
		}
		if t.Failed() {
			t.Logf("mock output:\n%s", output.String())
		}
	})

	// The GCS serves once it has started and the daemon answered; the
	// first start may also generate clips
	r.waitFor(30*time.Second, "the GCS to serve the vehicles", func() (bool, string) {
		select {
		case err := <-exited:
			exited <- err
			t.Fatalf("the mock exited early: %v\n%s", err, output.String())
		default:
		}
		resp, err := http.Get(r.url + "/api/remote/vehicles")
		if err != nil {
			return false, err.Error()
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		return resp.StatusCode == http.StatusOK, fmt.Sprintf("%d %s", resp.StatusCode, body)
	})
	children = childrenOf(cmd.Process.Pid)
	if len(children) == 0 {
		t.Fatal("the mock has no child process")
	}
	return r
}

// freePort returns a TCP port nothing listens on.
func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

// childrenOf returns the processes whose parent is pid.
func childrenOf(pid int) []int {
	stats, _ := filepath.Glob("/proc/[0-9]*/stat")
	var out []int
	for _, path := range stats {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		// pid (comm) state ppid ...; comm may contain spaces
		i := bytes.LastIndexByte(data, ')')
		if i < 0 {
			continue
		}
		fields := strings.Fields(string(data[i+1:]))
		if len(fields) < 2 || fields[1] != strconv.Itoa(pid) {
			continue
		}
		if child, err := strconv.Atoi(strings.Fields(string(data))[0]); err == nil {
			out = append(out, child)
		}
	}
	return out
}

// alive reports whether pid is a process that hasn't exited.
func alive(pid int) bool {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return false
	}
	i := bytes.LastIndexByte(data, ')')
	fields := strings.Fields(string(data[i+1:]))
	return len(fields) > 0 && fields[0] != "Z" && fields[0] != "X"
}

// lockedBuffer is a buffer that two writers may share.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// waitFor polls cond every pollPeriod until it holds, failing the test if it
// doesn't within limit. cond returns what it saw, for the failure.
func (r *mockRun) waitFor(limit time.Duration, what string, cond func() (bool, string)) {
	r.t.Helper()
	deadline := time.Now().Add(limit)
	var last string
	for {
		ok, seen := cond()
		if ok {
			return
		}
		last = seen
		if time.Now().After(deadline) {
			r.t.Fatalf("timed out after %v waiting for %s; last saw: %s", limit, what, last)
		}
		time.Sleep(pollPeriod)
	}
}

// getJSON decodes the response to a GET of path into v.
func (r *mockRun) getJSON(path string, v any) error {
	resp, err := http.Get(r.url + path)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("GET %s = %d %s", path, resp.StatusCode, body)
	}
	return json.Unmarshal(body, v)
}

// result is one call's result in a control response.
type result struct {
	Vehicle string `json:"vehicle"`
	Command string `json:"command"`
	Success bool   `json:"success"`
	Details string `json:"details"`
}

// post posts req as JSON to path, which must succeed, and returns the
// results.
func (r *mockRun) post(path string, req any) []result {
	r.t.Helper()
	body, err := json.Marshal(req)
	if err != nil {
		r.t.Fatal(err)
	}
	resp, err := http.Post(r.url+path, "application/json", bytes.NewReader(body))
	if err != nil {
		r.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		r.t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		r.t.Fatalf("POST %s %s = %d %s", path, body, resp.StatusCode, data)
	}
	var out struct {
		Results []result `json:"results"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		r.t.Fatalf("POST %s: %v in %s", path, err, data)
	}
	return out.Results
}

// postOK posts req to path, and fails unless every call succeeded.
func (r *mockRun) postOK(path string, req any, commands ...string) {
	r.t.Helper()
	results := r.post(path, req)
	var got []string
	for _, res := range results {
		got = append(got, res.Command)
		if !res.Success {
			r.t.Fatalf("POST %s: %s on %s failed: %s", path, res.Command, res.Vehicle, res.Details)
		}
	}
	if strings.Join(got, ",") != strings.Join(commands, ",") {
		r.t.Fatalf("POST %s: results for %v, want %v", path, got, commands)
	}
}

// vehicle is the part of /api/remote/vehicles the tests read.
type vehicle struct {
	Name    string `json:"name"`
	Current struct {
		Lat  float64 `json:"lat"`
		Long float64 `json:"long"`
		Alt  float64 `json:"alt"`
	} `json:"current"`
	GCSFlow string `json:"gcs_flow"`
}

// vehicles returns the GCS's vehicles by name.
func (r *mockRun) vehicles() (map[string]vehicle, error) {
	var list []vehicle
	if err := r.getJSON("/api/remote/vehicles", &list); err != nil {
		return nil, err
	}
	out := map[string]vehicle{}
	for _, v := range list {
		out[v.Name] = v
	}
	return out, nil
}

// videoLastSeen returns when the GCS last got a video frame from each
// vehicle.
func (r *mockRun) videoLastSeen() (map[string]time.Time, error) {
	var streams []struct {
		Vehicle  string    `json:"vehicle"`
		Kind     string    `json:"kind"`
		LastSeen time.Time `json:"last_seen"`
	}
	if err := r.getJSON("/api/streams", &streams); err != nil {
		return nil, err
	}
	out := map[string]time.Time{}
	for _, s := range streams {
		if s.Kind == "video" {
			out[s.Vehicle] = s.LastSeen
		}
	}
	return out, nil
}

// Video states of a vehicle across two polls a second apart.
const (
	videoAdvancing = "advancing"
	// videoStill is a stream present in both polls with the same last_seen.
	videoStill = "still"
	// videoMissing is a stream absent from either poll.
	videoMissing = "missing"
)

// videoStates returns each vehicle's video state, from two polls of
// /api/streams a second apart.
func (r *mockRun) videoStates() (map[string]string, string) {
	before, err := r.videoLastSeen()
	if err != nil {
		return nil, err.Error()
	}
	time.Sleep(time.Second)
	after, err := r.videoLastSeen()
	if err != nil {
		return nil, err.Error()
	}
	out := map[string]string{}
	var seen []string
	for vehicle, t := range after {
		b, ok := before[vehicle]
		switch {
		case !ok:
			out[vehicle] = videoMissing
		case t.After(b):
			out[vehicle] = videoAdvancing
		default:
			out[vehicle] = videoStill
		}
		seen = append(seen, fmt.Sprintf("%s %s->%s", vehicle, b.Format(time.TimeOnly+".000"), t.Format(time.TimeOnly+".000")))
	}
	return out, strings.Join(seen, "; ")
}

// waitVideo waits for each vehicle's video to be advancing (true) or present
// and still (false), as want says. A missing stream satisfies neither.
func (r *mockRun) waitVideo(limit time.Duration, what string, want map[string]bool) {
	r.t.Helper()
	r.waitFor(limit, what, func() (bool, string) {
		states, seen := r.videoStates()
		if states == nil {
			return false, seen
		}
		for vehicle, advancing := range want {
			wantState := videoStill
			if advancing {
				wantState = videoAdvancing
			}
			if states[vehicle] != wantState {
				return false, seen
			}
		}
		return true, seen
	})
}

// distanceMeters is the distance between two nearby points [meters].
func distanceMeters(lat1, lon1, lat2, lon2 float64) float64 {
	const earthRadius = 6371000.0
	dLat := (lat2 - lat1) * math.Pi / 180
	dLon := (lon2 - lon1) * math.Pi / 180 * math.Cos((lat1+lat2)/2*math.Pi/180)
	return earthRadius * math.Hypot(dLat, dLon)
}

func TestMockEndToEnd(t *testing.T) {
	r := startMock(t, false)
	all := []string{r.alpha, r.bravo, r.charlie}

	t.Log("the vehicles are listed")
	r.waitFor(10*time.Second, "3 vehicles", func() (bool, string) {
		vs, err := r.vehicles()
		if err != nil {
			return false, err.Error()
		}
		for _, name := range all {
			if _, ok := vs[name]; !ok {
				return false, fmt.Sprintf("%d vehicles, no %s", len(vs), name)
			}
		}
		return len(vs) == 3, fmt.Sprintf("%d vehicles", len(vs))
	})

	t.Log("each vehicle's video arrives, once the GCS has pushed its flows")
	r.waitVideo(15*time.Second, "video from every vehicle",
		map[string]bool{r.alpha: true, r.bravo: true, r.charlie: true})

	t.Log("a mission takes alpha's video")
	r.postOK("/api/start", map[string]any{"vehicles": []string{r.alpha}}, "StartMission")
	r.waitVideo(5*time.Second, "alpha's video to stop while bravo's goes on",
		map[string]bool{r.alpha: false, r.bravo: true})

	t.Log("hold brings it back")
	r.postOK("/api/command", map[string]any{"hold": true, "vehicles": []string{r.alpha}}, "Hold", "StopMission")
	r.waitVideo(5*time.Second, "alpha's video to return", map[string]bool{r.alpha: true})

	t.Log("alpha takes off and flies with the joystick")
	vs, err := r.vehicles()
	if err != nil {
		t.Fatal(err)
	}
	start := vs[r.alpha].Current
	r.postOK("/api/command", map[string]any{"takeoff": 5, "vehicles": []string{r.alpha}}, "TakeOff")
	time.Sleep(4 * time.Second)
	for end := time.Now().Add(2 * time.Second); time.Now().Before(end); {
		r.postOK("/api/joystick", map[string]any{"xvel": 5, "vehicles": []string{r.alpha}}, "SetVelocityTarget")
		time.Sleep(150 * time.Millisecond)
	}
	r.waitFor(5*time.Second, "alpha to move more than 5 m", func() (bool, string) {
		vs, err := r.vehicles()
		if err != nil {
			return false, err.Error()
		}
		now := vs[r.alpha].Current
		d := distanceMeters(start.Lat, start.Long, now.Lat, now.Long)
		return d > 5, fmt.Sprintf("moved %.1f m to %+v, alt %.1f m", d, now, now.Alt)
	})
}

func TestMockNoEncoder(t *testing.T) {
	r := startMock(t, true)
	r.waitFor(10*time.Second, "charlie's flow to have no video", func() (bool, string) {
		vs, err := r.vehicles()
		if err != nil {
			return false, err.Error()
		}
		v, ok := vs[r.charlie]
		if !ok {
			return false, fmt.Sprintf("%d vehicles, no %s", len(vs), r.charlie)
		}
		return strings.HasPrefix(v.GCSFlow, "no video:"), fmt.Sprintf("gcs_flow %q", v.GCSFlow)
	})
	// The vehicles on a daemon with an encoder still get video
	r.waitFor(10*time.Second, "alpha's flow to have video", func() (bool, string) {
		vs, err := r.vehicles()
		if err != nil {
			return false, err.Error()
		}
		return vs[r.alpha].GCSFlow == "ok", fmt.Sprintf("gcs_flow %q", vs[r.alpha].GCSFlow)
	})
}
