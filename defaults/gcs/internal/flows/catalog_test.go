package flows

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"google.golang.org/grpc"
)

const (
	vehicleConf = "[driver]\ntype=\"copter\"\n[[driver.cameras]]\nname=\"rgb\"\ntype=\"RawFrame\"\nprimary=true\n"
	yoloResult  = "steeleagle_protocol/v1/messages/result/DetectionResult"
	yoloConf    = "[engine]\ntype=\"detection\"\ninputs=[\"RawFrame\"]\nresult=\"" + yoloResult + "\"\n"
	encoderConf = "[engine]\ntype=\"generic\"\ninputs=[\"RawFrame\"]\n"
)

func svc(name string, cat daemonpb.ServiceCategory, conf string) *daemonpb.Service {
	return daemonpb.Service_builder{Name: name, Category: cat, Config: []byte(conf)}.Build()
}

const (
	vehicle = daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE
	engine  = daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE
)

func listResponse() *daemonpb.ListServicesResponse {
	return daemonpb.ListServicesResponse_builder{
		Services: []*daemonpb.Service{
			svc("100.64.0.2.yolo", engine, yoloConf),
			svc("100.64.0.2.charlie", vehicle, vehicleConf),
			svc("100.64.0.1.h264_encoder", engine, encoderConf),
			svc("100.64.0.1.alpha", vehicle, vehicleConf),
		},
		Remotes: []*daemonpb.Daemon{
			daemonpb.Daemon_builder{Ip: "100.64.0.2", Hostname: "swap03"}.Build(),
			daemonpb.Daemon_builder{Ip: "100.64.0.9", Error: "refused"}.Build(),
		},
	}.Build()
}

func TestBuildCatalogGroupsByDaemon(t *testing.T) {
	c := BuildCatalog(listResponse(), "100.64.0.1", "cloudlet033")
	if len(c.Daemons) != 2 {
		t.Fatalf("daemons %+v, want 2", c.Daemons)
	}
	local, remote := c.Daemons[0], c.Daemons[1]
	if local.IP != "100.64.0.1" || !local.Local || local.Hostname != "cloudlet033" {
		t.Errorf("local = %+v", local)
	}
	if remote.IP != "100.64.0.2" || remote.Local || remote.Hostname != "swap03" {
		t.Errorf("remote = %+v", remote)
	}
	if len(local.Vehicles) != 1 || local.Vehicles[0].Name != "100.64.0.1.alpha" || !local.Vehicles[0].Typed {
		t.Errorf("local vehicles = %+v", local.Vehicles)
	}
	y, ok := c.Engine("100.64.0.2.yolo")
	if !ok || y.Bare != "yolo" || y.Result != yoloResult || !y.Typed {
		t.Errorf("yolo = %+v, %v", y, ok)
	}
	if _, ok := c.Engine("yolo"); ok {
		t.Error("Engine finds a bare name")
	}
	if d, ok := c.Daemon("100.64.0.2"); !ok || d.Hostname != "swap03" {
		t.Errorf("Daemon = %+v, %v", d, ok)
	}
	if v, ok := c.VehicleByName("100.64.0.2.charlie"); !ok || len(v.Streams) != 2 || !v.Streams[1].Primary {
		t.Errorf("charlie = %+v, %v", v, ok)
	}
	if _, ok := c.VehicleByName("nobody"); ok {
		t.Error("VehicleByName finds a missing vehicle")
	}
	want := []fleet.Unavailable{{IP: "100.64.0.9", Hostname: "", Reason: "refused"}}
	if !reflect.DeepEqual(c.Unavailable, want) {
		t.Errorf("Unavailable = %+v, want %+v", c.Unavailable, want)
	}
}

func TestBuildCatalogUntypedConfig(t *testing.T) {
	resp := daemonpb.ListServicesResponse_builder{Services: []*daemonpb.Service{
		svc("100.64.0.1.bad", vehicle, "not = [toml"),
		svc("100.64.0.1.badengine", engine, ""),
	}}.Build()
	c := BuildCatalog(resp, "100.64.0.1", "cloudlet033")
	v, ok := c.VehicleByName("100.64.0.1.bad")
	if !ok || v.Typed || v.Problem == "" || len(v.Streams) != 1 || v.Streams[0].Name != "telemetry" {
		t.Errorf("vehicle = %+v, want untyped with only telemetry", v)
	}
	e, ok := c.Engine("100.64.0.1.badengine")
	if !ok || e.Typed || e.Problem == "" || !reflect.DeepEqual(e.Accepts, []string{"*"}) ||
		len(e.Events) != 1 || e.Events[0].Label != "result" || e.Bare != "badengine" {
		t.Errorf("engine = %+v, want untyped accepting anything", e)
	}
}

type fakeLister struct {
	resp *daemonpb.ListServicesResponse
	err  error
}

func (f fakeLister) ListServices(context.Context, *daemonpb.ListServicesRequest, ...grpc.CallOption) (*daemonpb.ListServicesResponse, error) {
	return f.resp, f.err
}

func TestServeCatalog(t *testing.T) {
	h := &CatalogHandler{daemon: fakeLister{resp: listResponse()}, gcsIP: "100.64.0.1"}
	w := httptest.NewRecorder()
	h.ServeCatalog(w, httptest.NewRequest(http.MethodGet, "/api/flows/catalog", nil))
	if w.Code != 200 {
		t.Fatalf("code %d", w.Code)
	}
	var got map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	ds := got["daemons"].([]any)
	d0 := ds[0].(map[string]any)
	if d0["ip"] != "100.64.0.1" || d0["local"] != true {
		t.Errorf("daemons[0] = %v", d0)
	}
	eng := d0["engines"].([]any)[0].(map[string]any)
	for _, k := range []string{"name", "bare", "category", "accepts", "result", "events", "typed"} {
		if _, ok := eng[k]; !ok {
			t.Errorf("engine JSON lacks %q: %v", k, eng)
		}
	}
	if u := got["unavailable"].([]any)[0].(map[string]any); u["reason"] != "refused" {
		t.Errorf("unavailable = %v", u)
	}
}

func TestServeCatalogListFailure(t *testing.T) {
	h := &CatalogHandler{daemon: fakeLister{err: errors.New("boom")}}
	w := httptest.NewRecorder()
	h.ServeCatalog(w, httptest.NewRequest(http.MethodGet, "/", nil))
	if w.Code != http.StatusBadGateway || w.Body.String() != "{\"detail\":\"boom\"}\n" {
		t.Errorf("got %d %q, want 502 and the detail", w.Code, w.Body)
	}
}
