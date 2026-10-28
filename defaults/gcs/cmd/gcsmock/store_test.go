package main

import (
	"strings"
	"testing"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// restartClock is a settable time for the mock's restart window.
type restartClock struct{ t time.Time }

func (c *restartClock) now() time.Time { return c.t }

func withClock(h *harness) *restartClock {
	c := &restartClock{t: time.Date(2026, 10, 28, 12, 0, 0, 0, time.UTC)}
	h.d.store.now = c.now
	return c
}

// remote returns the listed remote daemon at ip, and the services listed
// under it.
func remote(t *testing.T, h *harness, ip string) (*daemonpb.Daemon, []string) {
	t.Helper()
	resp, err := daemonpb.NewDaemonServiceClient(h.conn).ListServices(routed(""), &daemonpb.ListServicesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	var svcs []string
	for _, s := range resp.GetServices() {
		if strings.HasPrefix(s.GetName(), ip+".") {
			svcs = append(svcs, s.GetName())
		}
	}
	for _, r := range resp.GetRemotes() {
		if r.GetIp() == ip {
			return r, svcs
		}
	}
	t.Fatalf("no remote %s", ip)
	return nil, nil
}

const missingConfig = `[daemon]
hostname = "mock-100.64.0.2"

[[services]]
name = "yolo"
type = "engine"
[services.plugin]
plugin = "yolo"
package = "github.com/org/missing"
`

func TestMockConfigureRestartWindow(t *testing.T) {
	h := newHarness(t)
	c := withClock(h)
	client := daemonpb.NewDaemonServiceClient(h.conn)
	got, err := client.GetConfiguration(routed("100.64.0.2"), &daemonpb.GetConfigurationRequest{})
	if err != nil || !strings.Contains(string(got.GetConfigToml()), `name = "bravo"`) {
		t.Fatalf("config = %q, %v", got.GetConfigToml(), err)
	}
	if _, err := client.Configure(routed("100.64.0.2"), daemonpb.ConfigureRequest_builder{ConfigToml: "this is [not toml"}.Build()); status.Code(err) != codes.InvalidArgument {
		t.Errorf("invalid config: %v, want InvalidArgument", err)
	}
	changed := strings.Replace(string(got.GetConfigToml()), "mock-100.64.0.2", "renamed", 1)
	if _, err := client.Configure(routed("100.64.0.2"), daemonpb.ConfigureRequest_builder{ConfigToml: changed}.Build()); err != nil {
		t.Fatal(err)
	}
	if after, _ := client.GetConfiguration(routed("100.64.0.2"), &daemonpb.GetConfigurationRequest{}); string(after.GetConfigToml()) != changed {
		t.Errorf("config not stored: %q", after.GetConfigToml())
	}
	if r, svcs := remote(t, h, "100.64.0.2"); r.GetError() != "restarting" || len(svcs) != 0 {
		t.Errorf("during the window: error %q, services %v", r.GetError(), svcs)
	}
	c.t = c.t.Add(4 * time.Second)
	if r, svcs := remote(t, h, "100.64.0.2"); r.GetError() != "" || len(svcs) == 0 {
		t.Errorf("after the window: error %q, services %v", r.GetError(), svcs)
	}
	if _, err := client.Restart(routed("100.64.0.2"), &daemonpb.RestartRequest{}); err != nil {
		t.Fatal(err)
	}
	if r, _ := remote(t, h, "100.64.0.2"); r.GetError() != "restarting" {
		t.Errorf("after Restart: error %q", r.GetError())
	}
}

func TestMockPackages(t *testing.T) {
	h := newHarness(t)
	client := daemonpb.NewDaemonServiceClient(h.conn)
	uris := func() []string {
		resp, err := client.ListPackages(routed("100.64.0.2"), &daemonpb.ListPackagesRequest{})
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, p := range resp.GetPackages() {
			out = append(out, p.GetUri())
			if len(p.GetPlugins()) == 0 {
				t.Errorf("package %s has no plugins", p.GetUri())
			}
		}
		return out
	}
	if got := uris(); !contains(got, mockVehiclePackage) {
		t.Fatalf("packages = %v, want %s", got, mockVehiclePackage)
	}
	if _, err := client.InstallPackage(routed("100.64.0.2"), daemonpb.InstallPackageRequest_builder{Uri: "github.com/example/pkg"}.Build()); err != nil {
		t.Fatal(err)
	}
	if got := uris(); !contains(got, "github.com/example/pkg") {
		t.Errorf("after install: %v", got)
	}
	if _, err := client.RemovePackage(routed("100.64.0.2"), daemonpb.RemovePackageRequest_builder{Uri: "github.com/example/pkg"}.Build()); err != nil {
		t.Fatal(err)
	}
	if got := uris(); contains(got, "github.com/example/pkg") {
		t.Errorf("after remove: %v", got)
	}
	if _, err := client.RemovePackage(routed("100.64.0.2"), daemonpb.RemovePackageRequest_builder{Uri: "github.com/example/pkg"}.Build()); status.Code(err) != codes.NotFound {
		t.Errorf("second remove: %v, want NotFound", err)
	}
	// Packages are per daemon
	other, _ := client.ListPackages(routed("100.64.0.3"), &daemonpb.ListPackagesRequest{})
	for _, p := range other.GetPackages() {
		if p.GetUri() == "github.com/example/pkg" {
			t.Error("install leaked to another daemon")
		}
	}
}

func contains(list []string, s string) bool {
	for _, e := range list {
		if e == s {
			return true
		}
	}
	return false
}

func TestMockMissingPackageFailsServices(t *testing.T) {
	h := newHarness(t)
	c := withClock(h)
	client := daemonpb.NewDaemonServiceClient(h.conn)
	if _, err := client.Configure(routed("100.64.0.2"), daemonpb.ConfigureRequest_builder{ConfigToml: missingConfig}.Build()); err != nil {
		t.Fatal(err)
	}
	c.t = c.t.Add(4 * time.Second)
	r, svcs := remote(t, h, "100.64.0.2")
	if !strings.Contains(r.GetError(), "daemon failed to start services") || !strings.Contains(r.GetError(), "github.com/org/missing") || len(svcs) != 0 {
		t.Errorf("error %q, services %v", r.GetError(), svcs)
	}
	// Still configurable, which fixes it
	seed, _ := client.GetConfiguration(routed("100.64.0.3"), &daemonpb.GetConfigurationRequest{})
	fixed := strings.ReplaceAll(string(seed.GetConfigToml()), "100.64.0.3", "100.64.0.2")
	fixed = strings.ReplaceAll(fixed, "charlie", "bravo")
	if _, err := client.Configure(routed("100.64.0.2"), daemonpb.ConfigureRequest_builder{ConfigToml: fixed}.Build()); err != nil {
		t.Fatalf("configure a failed daemon: %v", err)
	}
}

func TestMockDenyConfig(t *testing.T) {
	h := newHarness(t, func(top *Topology) { top.DenyConfig = map[string]bool{"100.64.0.2": true} })
	client := daemonpb.NewDaemonServiceClient(h.conn)
	if _, err := client.GetConfiguration(routed("100.64.0.2"), &daemonpb.GetConfigurationRequest{}); err != nil {
		t.Errorf("reads stay allowed: %v", err)
	}
	calls := map[string]func() error{
		"Configure": func() error {
			_, err := client.Configure(routed("100.64.0.2"), daemonpb.ConfigureRequest_builder{ConfigToml: missingConfig}.Build())
			return err
		},
		"Restart": func() error {
			_, err := client.Restart(routed("100.64.0.2"), &daemonpb.RestartRequest{})
			return err
		},
		"InstallPackage": func() error {
			_, err := client.InstallPackage(routed("100.64.0.2"), daemonpb.InstallPackageRequest_builder{Uri: "u"}.Build())
			return err
		},
		"RemovePackage": func() error {
			_, err := client.RemovePackage(routed("100.64.0.2"), daemonpb.RemovePackageRequest_builder{Uri: "u"}.Build())
			return err
		},
	}
	for name, call := range calls {
		if err := call(); status.Code(err) != codes.PermissionDenied || !strings.Contains(status.Convert(err).Message(), "not allowed in state LOCKED") {
			t.Errorf("%s: %v, want PermissionDenied", name, err)
		}
	}
	if _, err := client.Restart(routed("100.64.0.3"), &daemonpb.RestartRequest{}); err != nil {
		t.Errorf("other daemons aren't denied: %v", err)
	}
}

func TestMockConfigUnknownRouteKey(t *testing.T) {
	h := newHarness(t)
	client := daemonpb.NewDaemonServiceClient(h.conn)
	if _, err := client.GetConfiguration(routed("100.64.0.77"), &daemonpb.GetConfigurationRequest{}); status.Code(err) != codes.NotFound {
		t.Errorf("unknown daemon: %v, want NotFound", err)
	}
	// No key is the GCS's own daemon
	got, err := client.GetConfiguration(routed(""), &daemonpb.GetConfigurationRequest{})
	if err != nil || !strings.Contains(string(got.GetConfigToml()), `name = "gcs"`) {
		t.Errorf("own config = %q, %v", got.GetConfigToml(), err)
	}
}
