package fleet

import (
	"context"
	"reflect"
	"runtime"
	"testing"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestSplitName(t *testing.T) {
	for _, tc := range []struct{ name, ip, bare string }{
		{"100.64.1.5.cassowary", "100.64.1.5", "cassowary"},
		{"fd7a:115c::1.harpy", "fd7a:115c::1", "harpy"},
		{"cassowary", "", "cassowary"},
		{"1.2.3.foo", "", "1.2.3.foo"},
	} {
		ip, bare := SplitName(tc.name)
		if ip != tc.ip || bare != tc.bare {
			t.Errorf("SplitName(%q) = (%q, %q), want (%q, %q)", tc.name, ip, bare, tc.ip, tc.bare)
		}
	}
}

const driverConfig = `
[plugin]
name = "quadcopter"
type = "driver"

[driver]
type = "copter"

[[driver.cameras]]
name = "front"
encode = true
`

const engineConfig = `
[plugin]
name = "detector"
type = "engine"

[engine]
`

func service(name string, category daemonpb.ServiceCategory, plugin string, conf string, running bool) *daemonpb.Service {
	return daemonpb.Service_builder{
		Name: name, Category: category, Plugin: plugin, Config: []byte(conf), Running: running,
	}.Build()
}

func TestList(t *testing.T) {
	d := newFakeDaemon(t)
	d.services = []*daemonpb.Service{
		service("100.64.1.5.cassowary", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "quadcopter", driverConfig, true),
		service("100.64.1.5.harpy", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "quadcopter", driverConfig, false),
		service("100.64.1.5.h264_encoder", daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE, "h264", "", true),
		service("100.64.1.5.extra", daemonpb.ServiceCategory_SERVICE_CATEGORY_EXTRA, "extra", "", true),
	}
	snap, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn()))
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Vehicles) != 1 {
		t.Fatalf("got vehicles %+v, want only the running one", snap.Vehicles)
	}
	v := snap.Vehicles[0]
	if v.Name != "100.64.1.5.cassowary" || v.Plugin != "quadcopter" || v.DaemonIP != "100.64.1.5" {
		t.Errorf("got vehicle %+v", v)
	}
	if v.DriverErr != nil || v.Driver == nil || v.Driver.Cameras[0].Name != "front" {
		t.Errorf("got driver %+v, err %v", v.Driver, v.DriverErr)
	}
	if len(snap.Engines) != 1 || !snap.Engines["100.64.1.5.h264_encoder"] {
		t.Errorf("got engines %v", snap.Engines)
	}
}

// TestListEncoders checks that each daemon's encoder is found by its plugin
// and package, whatever its service is named.
func TestListEncoders(t *testing.T) {
	encoder := func(name, uri string) *daemonpb.Service {
		return daemonpb.Service_builder{
			Name: name, Category: daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE,
			Plugin: EncoderPlugin, Uri: uri, Running: true,
		}.Build()
	}
	d := newFakeDaemon(t)
	d.services = []*daemonpb.Service{
		encoder("100.64.1.5.encoder", "github.com/cmusatyalab/steeleagle_plugins//defaults/h264@v4_backend_rework"),
		// The first by name of two encoders on a daemon
		encoder("100.64.1.6.h264_b", "github.com/cmusatyalab/steeleagle_plugins//defaults/h264"),
		encoder("100.64.1.6.h264_a", "github.com/cmusatyalab/steeleagle_plugins//defaults/h264"),
		// An encoder plugin from another package is not the H.264 encoder
		encoder("100.64.1.7.encoder", "github.com/example/plugins//jpeg"),
		service("100.64.1.7.h264_encoder", daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE, "h264", "", true),
	}
	snap, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn()))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"100.64.1.5": "100.64.1.5.encoder", "100.64.1.6": "100.64.1.6.h264_a"}
	if !reflect.DeepEqual(snap.Encoders, want) {
		t.Errorf("Encoders = %v, want %v", snap.Encoders, want)
	}
}

func TestListUnavailable(t *testing.T) {
	d := newFakeDaemon(t)
	d.remotes = []*daemonpb.Daemon{
		daemonpb.Daemon_builder{Ip: "100.64.1.7", Hostname: "swap03", Error: "connection refused"}.Build(),
		// A reachable remote is not unavailable
		daemonpb.Daemon_builder{Ip: "100.64.1.8", Hostname: "swap04", Arch: "arm64", Os: "linux"}.Build(),
	}
	snap, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn()))
	if err != nil {
		t.Fatal(err)
	}
	want := []Unavailable{{IP: "100.64.1.7", Hostname: "swap03", Reason: "connection refused"}}
	if !reflect.DeepEqual(snap.Unavailable, want) {
		t.Errorf("Unavailable = %+v, want %+v", snap.Unavailable, want)
	}
}

func TestListBadDriverConfig(t *testing.T) {
	d := newFakeDaemon(t)
	d.services = []*daemonpb.Service{
		service("a", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "p", "not toml", true),
		service("b", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "p", engineConfig, true),
	}
	snap, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn()))
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Vehicles) != 2 {
		t.Fatalf("got vehicles %+v, want both", snap.Vehicles)
	}
	for _, v := range snap.Vehicles {
		if v.DriverErr == nil || v.Driver != nil {
			t.Errorf("vehicle %s: got driver %+v, err %v; want an error and no driver", v.Name, v.Driver, v.DriverErr)
		}
	}
}

func TestListError(t *testing.T) {
	d := newFakeDaemon(t)
	d.listErr = status.Error(codes.Unavailable, "down")
	if _, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn())); err == nil {
		t.Fatal("got no error")
	}
}

func TestListKeepsRawConfig(t *testing.T) {
	d := newFakeDaemon(t)
	d.services = []*daemonpb.Service{
		service("a", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "p", driverConfig, true),
	}
	snap, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn()))
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Vehicles) != 1 || string(snap.Vehicles[0].Config) != driverConfig {
		t.Errorf("got vehicles %+v, want config %q", snap.Vehicles, driverConfig)
	}
}

func TestListSetsArchFromRemotes(t *testing.T) {
	d := newFakeDaemon(t)
	d.remotes = []*daemonpb.Daemon{
		daemonpb.Daemon_builder{Ip: "100.64.1.5", Hostname: "swap03", Arch: "arm64", Os: "linux"}.Build(),
		daemonpb.Daemon_builder{Ip: "100.64.1.6", Hostname: "old"}.Build(),
	}
	d.services = []*daemonpb.Service{
		service("100.64.1.5.remote", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "p", driverConfig, true),
		service("100.64.1.9.local", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "p", driverConfig, true),
		service("100.64.1.6.old", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "p", driverConfig, true),
	}
	snap, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn()))
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, v := range snap.Vehicles {
		got[v.Name] = v.Arch
	}
	want := map[string]string{"100.64.1.5.remote": "arm64", "100.64.1.9.local": runtime.GOARCH, "100.64.1.6.old": ""}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got arches %v, want %v", got, want)
	}
}
