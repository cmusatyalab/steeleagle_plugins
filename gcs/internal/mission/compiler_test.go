package mission

import (
	"bytes"
	"context"
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestResolveConfigDefaults(t *testing.T) {
	c, err := ResolveConfig(CompilerConfig{})
	if err != nil {
		t.Fatal(err)
	}
	if c.TimeoutS != 300 {
		t.Errorf("timeout = %v, want 300", c.TimeoutS)
	}
	var paths []string
	for _, i := range c.Imports {
		paths = append(paths, i.Path)
	}
	want := []string{"dsl/actions", "dsl/events", "dsl/datatypes", "params", "enums"}
	for i := range want {
		want[i] = sdkModule + "/" + want[i]
	}
	if !reflect.DeepEqual(paths, want) {
		t.Errorf("imports = %v, want %v", paths, want)
	}
	if len(c.Modules) != 1 || c.Modules[0].Path != sdkModule || c.Modules[0].Version == "" {
		t.Errorf("modules = %+v, want the SDK with a version", c.Modules)
	}
}

func TestResolveConfigSDKVersion(t *testing.T) {
	c, err := ResolveConfig(CompilerConfig{SDKVersion: "v1.2.3"})
	if err != nil {
		t.Fatal(err)
	}
	if c.Modules[0].Version != "v1.2.3" {
		t.Errorf("modules = %+v", c.Modules)
	}
}

func TestResolveConfigKeepsOverrides(t *testing.T) {
	c, err := ResolveConfig(CompilerConfig{
		SDKVersion: "v1.2.3",
		Modules: []Module{
			{Path: "example.com/other", Version: "v0.1.0"},
			{Path: sdkModule, Override: "/repo/sdk"},
		},
		Imports: []Import{
			{Path: sdkModule + "/params", Alias: "p"},
			{Path: "example.com/other/pkg", Alias: "o"},
			{Path: "example.com/other/pkg"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := c.Modules[0]; got.Path != sdkModule || got.Override != "/repo/sdk" || got.Version != "" {
		t.Errorf("sdk module = %+v, want the override", got)
	}
	if len(c.Modules) != 2 || c.Modules[1].Path != "example.com/other" {
		t.Errorf("modules = %+v", c.Modules)
	}
	if len(c.Imports) != 6 {
		t.Fatalf("imports = %+v, want 5 defaults + 1", c.Imports)
	}
	if got := c.Imports[3]; got.Alias != "" {
		t.Errorf("default import params got alias %q; defaults win", got.Alias)
	}
	if got := c.Imports[5]; got.Path != "example.com/other/pkg" || got.Alias != "o" {
		t.Errorf("last import = %+v", got)
	}
}

func TestHeaderShape(t *testing.T) {
	c := CompilerConfig{Modules: []Module{{Path: sdkModule, Override: "/repo/sdk"}}}
	if got, want := c.Header(), exHeader(t); !reflect.DeepEqual(got, want) {
		t.Errorf("Header() =\n%v\nwant\n%v", got, want)
	}
	// Resolving first changes nothing about the imports.
	r, err := ResolveConfig(c)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := r.Header(), exHeader(t); !reflect.DeepEqual(got, want) {
		t.Errorf("resolved Header() =\n%v\nwant\n%v", got, want)
	}
}

func TestCompilerTimeoutAbandons(t *testing.T) {
	c := NewSDKCompiler(CompilerConfig{TimeoutS: 0.05}, 1).(*sdkCompiler)
	release := make(chan struct{})
	c.compile = func(BuildRequest) ([]byte, error) {
		<-release
		return []byte("ok"), nil
	}
	ctx := context.Background()

	_, err := c.Build(ctx, BuildRequest{})
	if err == nil || !strings.Contains(err.Error(), "timed out after 0.05 s") {
		t.Fatalf("first Build err = %v", err)
	}
	// The abandoned compile still holds the only slot.
	start := time.Now()
	_, err = c.Build(ctx, BuildRequest{})
	if err == nil || !strings.Contains(err.Error(), "timed out after 0.05 s") {
		t.Fatalf("second Build err = %v", err)
	}
	if time.Since(start) > 2*time.Second {
		t.Errorf("second Build hung for %v", time.Since(start))
	}

	close(release)
	deadline := time.Now().Add(5 * time.Second)
	for {
		out, err := c.Build(ctx, BuildRequest{})
		if err == nil {
			if string(out) != "ok" {
				t.Fatalf("out = %q", out)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("third Build never succeeded: %v", err)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestCompilerContextCancel(t *testing.T) {
	c := NewSDKCompiler(CompilerConfig{TimeoutS: 60}, 1).(*sdkCompiler)
	release := make(chan struct{})
	defer close(release)
	c.compile = func(BuildRequest) ([]byte, error) { <-release; return nil, nil }
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(20 * time.Millisecond); cancel() }()
	if _, err := c.Build(ctx, BuildRequest{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
}

func TestSDKCompilerRealBuild(t *testing.T) {
	if os.Getenv("GCS_COMPILER_E2E") != "1" {
		t.Skip("set GCS_COMPILER_E2E=1 (needs network and go build)")
	}
	cfg, err := ResolveConfig(CompilerConfig{TimeoutS: 600})
	if err != nil {
		t.Fatal(err)
	}
	c := NewSDKCompiler(cfg, 2)
	ctx := context.Background()

	reg, err := c.Registry(ctx)
	if err != nil {
		t.Fatalf("Registry: %v", err)
	}
	if _, ok := reg["actions"]; !ok {
		t.Errorf("registry has no actions: keys %v", reg)
	}

	ast := exAST(t)
	ast["Modules"] = cfg.Header()["Modules"]
	ast["Imports"] = cfg.Header()["Imports"]
	req := BuildRequest{Ast: ast, GeoJSON: []byte(exPolyGeoJSON)}
	var first []byte
	for _, arch := range []string{"amd64", "arm64"} {
		req.Arch = arch
		bin, err := c.Build(ctx, req)
		if err != nil {
			t.Fatalf("Build %s: %v", arch, err)
		}
		if got, err := ElfArch(bin); err != nil || got != arch {
			t.Errorf("ElfArch = %q, %v; want %s", got, err, arch)
		}
		if arch == "amd64" {
			first = bin
		}
	}
	req.Arch = "amd64"
	again, err := c.Build(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(first, again) {
		t.Log("builds aren't reproducible; Deploy will always upload")
	} else {
		t.Log("builds are reproducible")
	}

	plugin, err := os.ReadFile("testdata/driver.plugin.toml")
	if err != nil {
		t.Fatal(err)
	}
	req.Plugin = plugin
	if _, err := c.Build(ctx, req); err != nil {
		t.Fatalf("Build with plugin: %v", err)
	}
}
