package flows

import (
	"strings"
	"testing"
)

const driverTOML = `
[plugin]
name = "x"
type = "driver"

[driver]
type = "copter"

[[driver.cameras]]
name = "rgb"
type = "steeleagle_protocol/v1/messages/frame/RawFrame"
description = "Main"
hres = 1080
vres = 720
fps = 30
primary = true

[[driver.cameras]]
name = "thermal"
type = "steeleagle_protocol/v1/messages/frame/RawFrame"
`

func TestParseDriverStreams(t *testing.T) {
	streams, problems, err := ParseDriver([]byte(driverTOML))
	if err != nil || len(problems) != 0 {
		t.Fatalf("err %v problems %v", err, problems)
	}
	if len(streams) != 3 || streams[0].Name != "telemetry" || streams[0].Kind != "telemetry" || streams[0].Type != TelemetryType {
		t.Fatalf("streams %+v, want telemetry first", streams)
	}
	rgb := streams[1]
	if rgb.Name != "rgb" || rgb.Kind != "camera" || !rgb.Primary || rgb.Description != "Main" ||
		rgb.HRes == nil || *rgb.HRes != 1080 || *rgb.VRes != 720 || *rgb.FPS != 30 {
		t.Errorf("rgb = %+v", rgb)
	}
	th := streams[2]
	if th.Primary || th.HRes != nil || th.VRes != nil || th.FPS != nil {
		t.Errorf("thermal = %+v, want no primary or resolution", th)
	}
}

func TestParseDriverTelemetryOnly(t *testing.T) {
	streams, _, err := ParseDriver([]byte("[plugin]\nname=\"x\"\ntype=\"driver\"\n[driver]\ntype=\"copter\"\n"))
	if err != nil || len(streams) != 1 || streams[0].Name != "telemetry" {
		t.Errorf("streams %+v err %v, want only telemetry", streams, err)
	}
}

func TestParseDriverSkipsBadCameras(t *testing.T) {
	_, problems, err := ParseDriver([]byte(`
[driver]
type = "copter"
[[driver.cameras]]
type = "t"
[[driver.cameras]]
name = "notype"
[[driver.cameras]]
name = "telemetry"
type = "t"
`))
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		"a camera has no name; skipped",
		`camera "notype" has no type; skipped`,
		`camera "telemetry" duplicates the telemetry stream; skipped`,
	}
	if strings.Join(problems, "|") != strings.Join(want, "|") {
		t.Errorf("problems %q, want %q", problems, want)
	}
}

func TestParseDriverErrors(t *testing.T) {
	for name, conf := range map[string]string{
		"invalid":   "not = [toml",
		"no driver": "[engine]\ntype=\"detection\"\ninputs=[\"a\"]\n",
		"empty":     "",
	} {
		if _, _, err := ParseDriver([]byte(conf)); err == nil {
			t.Errorf("%s: want an error", name)
		}
	}
}

func TestParseEngine(t *testing.T) {
	e, err := ParseEngine([]byte(`
[engine]
type = "detection"
inputs = ["steeleagle_protocol/v1/messages/frame/RawFrame", "b"]
result = "steeleagle_protocol/v1/messages/result/DetectionResult"
`))
	if err != nil {
		t.Fatal(err)
	}
	res := "steeleagle_protocol/v1/messages/result/DetectionResult"
	if e.Category == nil || *e.Category != "detection" || len(e.Accepts) != 2 || e.Result != res || !e.Typed {
		t.Errorf("engine = %+v", e)
	}
	if len(e.Events) != 1 || e.Events[0].Label != "result" || e.Events[0].Type == nil || *e.Events[0].Type != res {
		t.Errorf("events = %+v, want the result", e.Events)
	}
}

func TestParseEngineSink(t *testing.T) {
	e, err := ParseEngine([]byte("[engine]\ntype=\"\"\ninputs=[\"a\"]\n"))
	if err != nil {
		t.Fatal(err)
	}
	if e.Category != nil || e.Result != "" || len(e.Events) != 1 || e.Events[0].Label != "result" || e.Events[0].Type != nil {
		t.Errorf("engine = %+v, want no category and an untyped result", e)
	}
}

func TestParseEngineErrors(t *testing.T) {
	for name, conf := range map[string]string{
		"invalid":   "not = [toml",
		"no engine": "[driver]\ntype=\"copter\"\n",
		"no inputs": "[engine]\ntype=\"detection\"\n",
	} {
		if _, err := ParseEngine([]byte(conf)); err == nil {
			t.Errorf("%s: want an error", name)
		}
	}
}
