package fleet

import (
	"bytes"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	common "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/common"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// testTelemetry builds telemetry stamped at ts, with relative z and heading.
func testTelemetry(ts time.Time, z, heading float32) *telemetrypb.Telemetry {
	return telemetrypb.Telemetry_builder{
		Timestamp:   timestamppb.New(ts),
		BatteryInfo: telemetrypb.BatteryInfo_builder{Percentage: proto.Uint32(82)}.Build(),
		GpsInfo:     telemetrypb.GpsInfo_builder{Satellites: proto.Uint32(14)}.Build(),
		AlertInfo: telemetrypb.AlertInfo_builder{
			MagnetometerWarning: telemetrypb.AlertInfo_MAGNETOMETER_WARNING_PERTURBATIONS,
		}.Build(),
		PositionInfo: telemetrypb.PositionInfo_builder{
			Home: common.GlobalPosition_builder{Latitude: proto.Float64(40.1), Longitude: proto.Float64(-79.9)}.Build(),
			GlobalPosition: common.GlobalPosition_builder{
				Latitude: proto.Float64(40.2), Longitude: proto.Float64(-79.8),
				Altitude: proto.Float32(312), Heading: proto.Float32(heading),
			}.Build(),
			RelativePosition: common.RelativePosition_builder{Z: proto.Float32(z)}.Build(),
			VelocityBody: common.Velocity_builder{
				XVel: proto.Float32(1), YVel: proto.Float32(2), ZVel: proto.Float32(3), AngularVel: proto.Float32(4),
			}.Build(),
		}.Build(),
	}.Build()
}

func TestToVehicleJSON(t *testing.T) {
	now := time.Now()
	got := ToVehicleJSON(Vehicle{Name: v1, Plugin: "quadcopter"}, testTelemetry(now.Add(-2*time.Second), 31.5, -90), "ok", now)
	if got.Name != v1 || got.Model != "quadcopter" || got.Type != "UAV" || got.Selected || got.GCSFlow != "ok" {
		t.Errorf("identity fields = %+v", got)
	}
	if got.Battery != 82 || got.Sats != 14 || got.Mag != 1 {
		t.Errorf("battery/sats/mag = %d/%d/%d", got.Battery, got.Sats, got.Mag)
	}
	if got.Current != (Location{Lat: 40.2, Long: -79.8, Alt: 31.5}) {
		t.Errorf("current = %+v", got.Current)
	}
	if got.Home == nil || got.Home.Lat != 40.1 || got.Home.Long != -79.9 {
		t.Errorf("home = %+v", got.Home)
	}
	if got.Bearing != 270 {
		t.Errorf("bearing = %v, want 270", got.Bearing)
	}
	if got.Velocity == nil || *got.Velocity != (Velocity{1, 2, 3, 4}) {
		t.Errorf("velocity = %+v", got.Velocity)
	}
	if math.Abs(got.LastUpdated-2) > 0.01 {
		t.Errorf("last_updated = %v, want ~2", got.LastUpdated)
	}
}

func TestToVehicleJSONNegativeAlt(t *testing.T) {
	now := time.Now()
	got := ToVehicleJSON(Vehicle{Name: v1}, testTelemetry(now, -0.3, 0), "", now)
	if got.Current.Alt != 0 {
		t.Errorf("alt = %v, want 0", got.Current.Alt)
	}
}

func TestToVehicleJSONNil(t *testing.T) {
	got := ToVehicleJSON(Vehicle{Name: v1}, nil, "", time.Now())
	if got.LastUpdated != 1e6 || got.Current != (Location{}) || got.Home != nil || got.Velocity != nil {
		t.Errorf("got %+v", got)
	}
	b, _ := json.Marshal(got)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	if _, ok := m["home"]; ok {
		t.Error("home present in JSON")
	}
	if _, ok := m["current"]; !ok {
		t.Error("current missing from JSON")
	}
}

// vehiclesFixture is a fake daemon with two vehicles with telemetry, and a
// Vehicles handler for it.
func vehiclesFixture(t *testing.T) (*fakeDaemon, *Flows, *Vehicles) {
	t.Helper()
	d, f, _ := flowsFixture(t)
	d.telemetry[v1] = testTelemetry(time.Now(), 10, 0)
	d.telemetry[v2] = testTelemetry(time.Now(), 20, 0)
	return d, f, NewVehicles(d.conn(), f, zerolog.Nop())
}

func getVehicles(t *testing.T, h *Vehicles) (int, []VehicleJSON, string) {
	t.Helper()
	w := httptest.NewRecorder()
	h.ServeVehicles(w, httptest.NewRequest(http.MethodGet, "/api/remote/vehicles", nil))
	var out []VehicleJSON
	if w.Code == http.StatusOK {
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatalf("body %q: %v", w.Body, err)
		}
	}
	return w.Code, out, w.Body.String()
}

func TestVehiclesEndpoint(t *testing.T) {
	_, f, h := vehiclesFixture(t)
	code, out, _ := getVehicles(t, h)
	f.Wait()
	if code != 200 || len(out) != 2 || out[0].Name != v1 || out[1].Name != v2 {
		t.Fatalf("code %d out %+v", code, out)
	}
	if out[0].Current.Alt != 10 || out[1].Current.Alt != 20 {
		t.Errorf("alts = %v, %v; want telemetry routed per vehicle", out[0].Current.Alt, out[1].Current.Alt)
	}
}

func TestVehiclesNoTelemetry(t *testing.T) {
	d, f, h := vehiclesFixture(t)
	delete(d.telemetry, v2)
	code, out, _ := getVehicles(t, h)
	f.Wait()
	if code != 200 || len(out) != 2 || out[1].LastUpdated < 1e6 {
		t.Fatalf("code %d out %+v", code, out)
	}
}

func TestVehiclesSlowVehicle(t *testing.T) {
	d, f, h := vehiclesFixture(t)
	d.blockTelemetry[v2] = true
	start := time.Now()
	code, out, _ := getVehicles(t, h)
	if el := time.Since(start); el > 1500*time.Millisecond {
		t.Errorf("took %v", el)
	}
	f.Wait()
	if code != 200 || len(out) != 2 || out[1].LastUpdated < 1e6 || out[0].LastUpdated > 100 {
		t.Fatalf("code %d out %+v", code, out)
	}
}

func TestVehiclesListError(t *testing.T) {
	d, _, h := vehiclesFixture(t)
	d.listErr = errNoDriver
	code, _, body := getVehicles(t, h)
	var e map[string]string
	_ = json.Unmarshal([]byte(body), &e)
	if code != http.StatusBadGateway || e["detail"] == "" {
		t.Errorf("code %d body %q", code, body)
	}
}

func TestVehiclesEmpty(t *testing.T) {
	d, _, h := vehiclesFixture(t)
	d.services = []*daemonpb.Service{}
	w := httptest.NewRecorder()
	h.ServeVehicles(w, httptest.NewRequest(http.MethodGet, "/", nil))
	if w.Code != 200 || w.Body.String() != "[]\n" {
		t.Errorf("code %d body %q", w.Code, w.Body)
	}
}

func TestVehiclesFlowStatus(t *testing.T) {
	_, f, h := vehiclesFixture(t)
	getVehicles(t, h)
	f.Wait()
	_, out, _ := getVehicles(t, h)
	f.Wait()
	if len(out) != 2 || out[0].GCSFlow != "ok" {
		t.Errorf("out %+v", out)
	}
}

// unavailable lists the fixture's daemon as unavailable, with its vehicles
// gone.
func unavailable(d *fakeDaemon) {
	d.services = []*daemonpb.Service{}
	d.remotes = []*daemonpb.Daemon{
		daemonpb.Daemon_builder{Ip: "100.64.1.5", Hostname: "swap03", Error: "connection refused"}.Build(),
	}
}

func TestVehiclesDaemonUnavailable(t *testing.T) {
	d, f, h := vehiclesFixture(t)
	getVehicles(t, h)
	f.Wait()
	unavailable(d)
	code, out, _ := getVehicles(t, h)
	f.Wait()
	if code != 200 || len(out) != 2 || out[0].Name != v1 || out[1].Name != v2 {
		t.Fatalf("code %d out %+v, want both vehicles still listed", code, out)
	}
	want := VehicleJSON{
		Name: v1, Model: "quadcopter", Type: "UAV", LastUpdated: staleSeconds,
		GCSFlow: "error: daemon 100.64.1.5 unavailable: connection refused",
	}
	if !reflect.DeepEqual(out[0], want) {
		t.Errorf("vehicle = %+v, want %+v", out[0], want)
	}

	// Still unavailable: still listed
	_, out, _ = getVehicles(t, h)
	if len(out) != 2 {
		t.Errorf("out %+v, want both vehicles still listed", out)
	}

	// The daemon is gone entirely: its vehicles are dropped
	d.remotes = nil
	_, out, _ = getVehicles(t, h)
	if len(out) != 0 {
		t.Errorf("out %+v, want no vehicles", out)
	}
	// and not remembered if it becomes unavailable again
	unavailable(d)
	_, out, _ = getVehicles(t, h)
	if len(out) != 0 {
		t.Errorf("out %+v, want no vehicles", out)
	}
}

func TestVehiclesDaemonBackWithoutVehicle(t *testing.T) {
	d, f, h := vehiclesFixture(t)
	getVehicles(t, h)
	f.Wait()
	unavailable(d)
	getVehicles(t, h)
	// The daemon is back, running only v1: v2 is dropped
	d.remotes = nil
	d.services = []*daemonpb.Service{
		service(v1, daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "quadcopter", driverConfig, true),
	}
	_, out, _ := getVehicles(t, h)
	f.Wait()
	if len(out) != 1 || out[0].Name != v1 || out[0].LastUpdated > 100 {
		t.Errorf("out %+v, want only a live %s", out, v1)
	}
}

func TestVehiclesLogsUnavailableOnce(t *testing.T) {
	d, f, _ := vehiclesFixture(t)
	var buf syncBuffer
	h := NewVehicles(d.conn(), f, zerolog.New(&buf))
	unavailable(d)
	for range 3 {
		getVehicles(t, h)
	}
	f.Wait()
	logs := buf.String()
	if n := strings.Count(logs, "connection refused"); n != 1 {
		t.Errorf("logged the unavailable daemon %d times, want once:\n%s", n, logs)
	}
	if !strings.Contains(logs, "100.64.1.5") || !strings.Contains(logs, "swap03") {
		t.Errorf("log %q lacks the daemon's IP and hostname", logs)
	}
	// A new reason is logged again
	d.remotes[0].SetError("timed out")
	getVehicles(t, h)
	if n := strings.Count(buf.String(), "timed out"); n != 1 {
		t.Errorf("logged the new reason %d times, want once:\n%s", n, buf.String())
	}
}

// syncBuffer is a bytes.Buffer safe for concurrent use.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}
