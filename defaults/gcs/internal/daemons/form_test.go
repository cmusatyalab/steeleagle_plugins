package daemons

import (
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/BurntSushi/toml"
)

func TestParseFormGlide(t *testing.T) {
	raw, err := os.ReadFile("testdata/glide.toml")
	if err != nil {
		t.Fatal(err)
	}
	f, err := ParseForm(raw)
	if err != nil {
		t.Fatal(err)
	}
	if f.Daemon.Port == nil || *f.Daemon.Port != 9000 {
		t.Errorf("port = %v, want 9000", f.Daemon.Port)
	}
	if f.Daemon.Hostname == nil || *f.Daemon.Hostname != "glide" {
		t.Errorf("hostname = %v, want glide", f.Daemon.Hostname)
	}
	if f.Simulator == nil || f.Simulator.Package != "https://github.com/cmusatyalab/steeleagle_plugins//simulator@v4_backend_rework" {
		t.Errorf("simulator = %+v", f.Simulator)
	}
	if len(f.Services) != 3 {
		t.Fatalf("got %d services, want 3", len(f.Services))
	}
	if got := f.Services[2].Plugin.Args["mapbox"]; got != "pk.test" {
		t.Errorf("gcs mapbox arg = %v, want pk.test", got)
	}
	if f.Services[2].Auth != "admin" {
		t.Errorf("gcs auth = %q, want admin", f.Services[2].Auth)
	}
}

func decodeMap(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if _, err := toml.Decode(string(raw), &m); err != nil {
		t.Fatalf("decode %q: %v", raw, err)
	}
	return m
}

func TestRoundTripKeepsUnknown(t *testing.T) {
	in := []byte(`
[daemon]
port = 9000
future = 1

[extra_table]
a = "b"

[[services]]
name = "yolo"
type = "engine"
weight = 2
[services.plugin]
plugin = "yolo"
package = "github.com/org/repo//yolo"
pin = true
[services.plugin.args]
threshold = 0.5
`)
	f, err := ParseForm(in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := RenderForm(f)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := decodeMap(t, out), decodeMap(t, in); !reflect.DeepEqual(got, want) {
		t.Errorf("round trip =\n%v\nwant\n%v\nrendered:\n%s", got, want, out)
	}
}

func TestRoundTripKeepsIntegers(t *testing.T) {
	body := `{"services":[{"name":"e","type":"engine","plugin":{"plugin":"p","package":"q","args":{"port":9000,"ratio":0.5,"list":[1,2]}}}]}`
	f, err := DecodeFormJSON(strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	out, err := RenderForm(f)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"port = 9000\n", "ratio = 0.5\n", "list = [1, 2]\n"} {
		if !strings.Contains(string(out), want) {
			t.Errorf("rendered config lacks %q:\n%s", want, out)
		}
	}
}

func TestParseFormAbsentVsEmpty(t *testing.T) {
	in := []byte(`
[daemon]
hostname = "h"

[[services]]
name = "e"
type = "engine"
[services.plugin]
plugin = "p"
package = "q"
`)
	f, err := ParseForm(in)
	if err != nil {
		t.Fatal(err)
	}
	if f.Simulator != nil {
		t.Errorf("simulator = %+v, want nil", f.Simulator)
	}
	if f.Daemon.Port != nil {
		t.Errorf("port = %v, want absent", *f.Daemon.Port)
	}
	if f.Services[0].Plugin.Args != nil {
		t.Errorf("args = %v, want nil", f.Services[0].Plugin.Args)
	}
	out, err := RenderForm(f)
	if err != nil {
		t.Fatal(err)
	}
	m := decodeMap(t, out)
	if _, ok := m["simulator"]; ok {
		t.Errorf("rendered a [simulator]:\n%s", out)
	}
	if _, ok := m["daemon"].(map[string]any)["port"]; ok {
		t.Errorf("rendered a port:\n%s", out)
	}
	plugin := m["services"].([]map[string]any)[0]["plugin"].(map[string]any)
	if _, ok := plugin["args"]; ok {
		t.Errorf("rendered args:\n%s", out)
	}
}

func TestParseFormUnrepresentable(t *testing.T) {
	cases := map[string]struct{ in, cause string }{
		"datetime": {`
[[services]]
name = "e"
type = "engine"
[services.plugin]
plugin = "p"
package = "q"
[services.plugin.args]
when = 1979-05-27T07:32:00Z
`, "date"},
		"services not tables": {`services = "x"`, "services"},
		"daemon not a table":  {`daemon = 3`, "daemon"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := ParseForm([]byte(c.in))
			if !errors.Is(err, ErrUnrepresentable) {
				t.Fatalf("err = %v, want ErrUnrepresentable", err)
			}
			if !strings.Contains(err.Error(), c.cause) {
				t.Errorf("err = %q, want it to mention %q", err, c.cause)
			}
		})
	}
}

func TestParseFormSyntaxError(t *testing.T) {
	_, err := ParseForm([]byte("[daemon"))
	var te *TOMLError
	if !errors.As(err, &te) {
		t.Fatalf("err = %v (%T), want *TOMLError", err, err)
	}
	if te.Line != 1 {
		t.Errorf("line = %d, want 1", te.Line)
	}
}

// TestParseFormEnvironment checks that a service's pixi environment is a
// field of its plugin, written back only when set.
func TestParseFormEnvironment(t *testing.T) {
	in := []byte(`
[[services]]
name = "yolo"
type = "engine"
[services.plugin]
plugin = "yolo"
package = "github.com/org/repo//yolo"
environment = "cuda126"
`)
	f, err := ParseForm(in)
	if err != nil {
		t.Fatal(err)
	}
	p := f.Services[0].Plugin
	if p.Environment != "cuda126" || p.Extra != nil {
		t.Fatalf("plugin = %+v, want environment cuda126 and no extra keys", p)
	}
	out, err := RenderForm(f)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := decodeMap(t, out), decodeMap(t, in); !reflect.DeepEqual(got, want) {
		t.Errorf("round trip =\n%v\nwant\n%v", got, want)
	}
	f.Services[0].Plugin.Environment = ""
	if out, err = RenderForm(f); err != nil {
		t.Fatal(err)
	}
	plugin := decodeMap(t, out)["services"].([]map[string]any)[0]["plugin"].(map[string]any)
	if _, ok := plugin["environment"]; ok {
		t.Errorf("rendered an empty environment:\n%s", out)
	}
}
