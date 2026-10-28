package mission

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
)

type sendCall struct {
	vehicle, name string
	binary        []byte
}

// fakeSender is a VehicleSender double; fn, if set, decides the outcome.
type fakeSender struct {
	mu    sync.Mutex
	calls []sendCall
	fn    func(ctx context.Context, vehicle string, progress Progress) (string, error)
}

func (f *fakeSender) Send(ctx context.Context, vehicle, name string, binary []byte, progress Progress) (string, error) {
	f.mu.Lock()
	f.calls = append(f.calls, sendCall{vehicle, name, binary})
	f.mu.Unlock()
	if f.fn != nil {
		return f.fn(ctx, vehicle, progress)
	}
	if progress != nil {
		progress(1, 2)
		progress(2, 2)
	}
	return "", nil
}

func (f *fakeSender) called() []sendCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]sendCall(nil), f.calls...)
}

func (f *fakeSender) binaryFor(t testing.TB, vehicle string) []byte {
	t.Helper()
	for _, c := range f.called() {
		if c.vehicle == vehicle {
			return c.binary
		}
	}
	t.Fatalf("no send to %s", vehicle)
	return nil
}

var (
	amdBin = append(fakeElf(emX86_64), "amd"...)
	armBin = append(fakeElf(emAArch64), "arm"...)
)

func vehicle(name, arch string) fleet.Vehicle {
	ip, _ := fleet.SplitName(name)
	return fleet.Vehicle{Name: name, DaemonIP: ip, Arch: arch}
}

func lister(vs ...fleet.Vehicle) Lister {
	return catalogLister(flows.Catalog{}, vs...)
}

// catalogLister lists vs, and cat as the flow catalog.
func catalogLister(cat flows.Catalog, vs ...fleet.Vehicle) Lister {
	return func(context.Context) ([]fleet.Vehicle, flows.Catalog, error) { return vs, cat, nil }
}

type uploadFile struct {
	name string
	data []byte
}

func multipartReq(t testing.TB, files []uploadFile, vehicles []string, name string) *http.Request {
	t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	for _, f := range files {
		fw, err := mw.CreateFormFile("files", f.name)
		if err != nil {
			t.Fatal(err)
		}
		fw.Write(f.data)
	}
	for _, v := range vehicles {
		mw.WriteField("vehicles", v)
	}
	if name != "" {
		mw.WriteField("name", name)
	}
	mw.Close()
	req := httptest.NewRequest(http.MethodPost, "/api/upload", &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	return req
}

func newUploads(t testing.TB, l Lister, s VehicleSender, f *fakeCompiler) *Uploads {
	t.Helper()
	if f == nil {
		f = newFake(t)
	}
	return NewUploads(l, s, newService(t, f), zerolog.Nop())
}

// newFlowUploads is newUploads whose Service lists the catalog l lists.
func newFlowUploads(t testing.TB, l Lister, s VehicleSender, f *fakeCompiler) *Uploads {
	t.Helper()
	_, cat, _ := l(context.Background())
	return NewUploads(l, s, newFlowService(t, f, cat), zerolog.Nop())
}

func events(t testing.TB, rec *httptest.ResponseRecorder) []map[string]any {
	t.Helper()
	body := rec.Body.String()
	if body != "" && !strings.HasSuffix(body, "\n") {
		t.Fatalf("body does not end in newline: %q", body)
	}
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSuffix(body, "\n"), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("line %q: %v", line, err)
		}
		out = append(out, m)
	}
	return out
}

func results(evs []map[string]any) map[string]map[string]any {
	out := map[string]map[string]any{}
	for _, e := range evs {
		if e["type"] == "result" {
			out[e["vehicle"].(string)] = e
		}
	}
	return out
}

func detail(t testing.TB, rec *httptest.ResponseRecorder) string {
	t.Helper()
	return decodeMap(t, rec)["detail"].(string)
}

func TestUploadRejects(t *testing.T) {
	other := append(fakeElf(emX86_64), "other"...)
	tests := []struct {
		name     string
		files    []uploadFile
		vehicles []string
		mname    string
		want     string
	}{
		{"no vehicles", []uploadFile{{"a", amdBin}}, nil, "m", "Select at least one vehicle"},
		{"bad name", []uploadFile{{"a", amdBin}}, []string{"v"}, ".hidden", "mission name must be"},
		{"missing name", []uploadFile{{"a", amdBin}}, []string{"v"}, "", "mission name must be"},
		{"no files", nil, []string{"v"}, "m", "Select at least one mission binary"},
		{"too many", []uploadFile{{"a", amdBin}, {"b", armBin}, {"c", amdBin}}, []string{"v"}, "m", "At most two"},
		{"not elf", []uploadFile{{"notes.txt", []byte("hello there, not a binary")}}, []string{"v"}, "m", "notes.txt: "},
		{"same arch", []uploadFile{{"a", amdBin}, {"b", other}}, []string{"v"}, "m", "Two files were built for amd64"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			s := &fakeSender{}
			u := newUploads(t, lister(vehicle("100.64.0.1.v", "amd64")), s, nil)
			rec := httptest.NewRecorder()
			u.ServeUpload(rec, multipartReq(t, tc.files, tc.vehicles, tc.mname))
			if rec.Code != 400 || !strings.Contains(detail(t, rec), tc.want) {
				t.Fatalf("got %d %s, want 400 containing %q", rec.Code, rec.Body, tc.want)
			}
			if len(s.called()) != 0 {
				t.Fatal("sender was called")
			}
		})
	}
}

func TestUploadRejectsBadName(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(vehicle("100.64.0.1.v", "amd64")), s, nil)
	for _, n := range []string{"has space", "a/b", strings.Repeat("a", 65)} {
		rec := httptest.NewRecorder()
		u.ServeUpload(rec, multipartReq(t, []uploadFile{{"a", amdBin}}, []string{"100.64.0.1.v"}, n))
		if rec.Code != 400 {
			t.Errorf("name %q: got %d", n, rec.Code)
		}
	}
	if len(s.called()) != 0 {
		t.Fatal("sender was called")
	}
}

func TestUploadRejectsOversize(t *testing.T) {
	u := newUploads(t, lister(), &fakeSender{}, nil)
	u.maxSize = 1 << 20
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"huge.bin", append(amdBin, make([]byte, 1<<20)...)}}, []string{"v"}, "m"))
	if rec.Code != 400 || !strings.Contains(detail(t, rec), "huge.bin is larger than the 1 MiB limit") {
		t.Fatalf("got %d %s", rec.Code, rec.Body)
	}
}

func TestUploadMapsFilesByElfNotFilename(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(vehicle("100.64.0.1.a", "arm64"), vehicle("100.64.0.1.b", "amd64")), s, nil)
	rec := httptest.NewRecorder()
	// File names are swapped relative to their contents.
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"mission-amd64", armBin}, {"mission-arm64", amdBin}},
		[]string{"100.64.0.1.a", "100.64.0.1.b"}, "m"))
	if rec.Code != 200 {
		t.Fatal(rec.Code, rec.Body)
	}
	if !bytes.Equal(s.binaryFor(t, "100.64.0.1.a"), armBin) || !bytes.Equal(s.binaryFor(t, "100.64.0.1.b"), amdBin) {
		t.Fatal("wrong binaries")
	}
}

func TestUploadMatchesFilesToVehicleArch(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(vehicle("100.64.0.1.a", "arm64"), vehicle("100.64.0.1.b", "amd64"), vehicle("100.64.0.1.c", "arm64")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"x", amdBin}}, []string{"100.64.0.1.a", "100.64.0.1.b", "100.64.0.1.c"}, "m"))
	res := results(events(t, rec))
	if res["100.64.0.1.b"]["success"] != true {
		t.Fatal(res)
	}
	for _, n := range []string{"100.64.0.1.a", "100.64.0.1.c"} {
		if res[n]["success"] != false || res[n]["details"] != "no binary for arm64" {
			t.Fatalf("%s: %v", n, res[n])
		}
	}
	if c := s.called(); len(c) != 1 || c[0].vehicle != "100.64.0.1.b" || c[0].name != "m" {
		t.Fatalf("calls %v", c)
	}
}

func TestUploadUnknownArch(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(vehicle("100.64.0.2.v", "")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"x", amdBin}}, []string{"100.64.0.2.v"}, "m"))
	r := results(events(t, rec))["100.64.0.2.v"]
	if r["success"] != false || r["details"] != "daemon 100.64.0.2 didn't report its architecture" {
		t.Fatal(r)
	}
	if len(s.called()) != 0 {
		t.Fatal("sender was called")
	}
}

func TestUploadDedupesAndReportsMissingVehicles(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(vehicle("100.64.0.1.a", "amd64")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"x", amdBin}},
		[]string{"100.64.0.1.a", "ghost", "100.64.0.1.a", "ghost"}, "m"))
	evs := events(t, rec)
	if len(s.called()) != 1 {
		t.Fatalf("calls %v", s.called())
	}
	n := 0
	for _, e := range evs {
		if e["type"] == "result" {
			n++
		}
	}
	if n != 2 {
		t.Fatalf("want 2 results, got %v", evs)
	}
	res := results(evs)
	if res["ghost"]["success"] != false || res["ghost"]["details"] != "not found" || res["100.64.0.1.a"]["success"] != true {
		t.Fatal(res)
	}
	// Failures are reported before the upload stage starts.
	if evs[0]["type"] != "result" || evs[0]["vehicle"] != "ghost" {
		t.Fatalf("first event %v", evs[0])
	}
}

func TestUploadRepeatedVehicleFields(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(vehicle("100.64.0.1.a", "amd64"), vehicle("100.64.0.1.b", "amd64")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"x", amdBin}}, []string{"100.64.0.1.a", "100.64.0.1.b"}, "m"))
	if len(s.called()) != 2 {
		t.Fatal(s.called())
	}
}

func TestUploadListFailureIs503(t *testing.T) {
	l := func(context.Context) ([]fleet.Vehicle, flows.Catalog, error) {
		return nil, flows.Catalog{}, errors.New("daemon down")
	}
	u := newUploads(t, l, &fakeSender{}, nil)
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"x", amdBin}}, []string{"v"}, "m"))
	if rec.Code != 503 {
		t.Fatal(rec.Code, rec.Body)
	}
}

func TestUploadOneVehicleFailsOthersSucceed(t *testing.T) {
	s := &fakeSender{fn: func(_ context.Context, v string, _ Progress) (string, error) {
		if v == "100.64.0.1.b" {
			return "", errors.New("upload failed: Unavailable - gone")
		}
		return DetailsAlreadyLoaded, nil
	}}
	u := newUploads(t, lister(vehicle("100.64.0.1.a", "amd64"), vehicle("100.64.0.1.b", "amd64")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"x", amdBin}}, []string{"100.64.0.1.a", "100.64.0.1.b"}, "m"))
	res := results(events(t, rec))
	if res["100.64.0.1.a"]["success"] != true || res["100.64.0.1.a"]["details"] != DetailsAlreadyLoaded {
		t.Fatal(res)
	}
	if res["100.64.0.1.b"]["success"] != false || res["100.64.0.1.b"]["details"] != "upload failed: Unavailable - gone" {
		t.Fatal(res)
	}
}

func TestUploadClientDisconnectCancels(t *testing.T) {
	sawCancel := make(chan error, 1)
	started := make(chan struct{})
	s := &fakeSender{fn: func(ctx context.Context, _ string, _ Progress) (string, error) {
		close(started)
		<-ctx.Done()
		sawCancel <- ctx.Err()
		return "", ctx.Err()
	}}
	u := newUploads(t, lister(vehicle("100.64.0.1.a", "amd64")), s, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := multipartReq(t, []uploadFile{{"x", amdBin}}, []string{"100.64.0.1.a"}, "m").WithContext(ctx)
	done := make(chan struct{})
	go func() {
		u.ServeUpload(httptest.NewRecorder(), req)
		close(done)
	}()
	<-started
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("handler did not return after the client went away")
	}
	if err := <-sawCancel; err == nil {
		t.Fatal("sender context was not cancelled")
	}
}

func TestStreamEventOrder(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(vehicle("100.64.0.1.a", "amd64")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeUpload(rec, multipartReq(t, []uploadFile{{"x", amdBin}}, []string{"100.64.0.1.a"}, "m"))
	if ct := rec.Header().Get("Content-Type"); ct != "application/x-ndjson" {
		t.Fatal(ct)
	}
	if !rec.Flushed {
		t.Fatal("not flushed")
	}
	want := []map[string]any{
		{"type": "stage", "stage": "uploading"},
		{"type": "progress", "vehicle": "100.64.0.1.a", "sent": float64(1), "total": float64(2)},
		{"type": "progress", "vehicle": "100.64.0.1.a", "sent": float64(2), "total": float64(2)},
		{"type": "result", "vehicle": "100.64.0.1.a", "success": true, "details": ""},
	}
	if got := events(t, rec); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v", got)
	}
}

// --- deploy ---

func deployReq(t testing.TB, vehicles []string, name string) *http.Request {
	t.Helper()
	body := graphBody(t, func(m map[string]any) {
		m["vehicles"] = vehicles
		if name != "" {
			m["name"] = name
		}
	})
	return httptest.NewRequest(http.MethodPost, "/api/deploy", bytes.NewReader(body))
}

func cfgVehicle(name, arch, cfg string) fleet.Vehicle {
	v := vehicle(name, arch)
	v.Config = []byte(cfg)
	return v
}

func TestDeployRejects(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(), s, nil)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, nil, "m"))
	if rec.Code != 400 || detail(t, rec) != "Select at least one vehicle" {
		t.Fatal(rec.Code, rec.Body)
	}
	rec = httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"v"}, "bad name"))
	if rec.Code != 400 || !strings.Contains(detail(t, rec), "mission name must be") {
		t.Fatal(rec.Code, rec.Body)
	}
}

func TestDeploySetupFailureIs503(t *testing.T) {
	f := newFake(t)
	f.registryErr = errors.New("no go")
	u := newUploads(t, lister(), &fakeSender{}, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"v"}, "m"))
	if rec.Code != 503 || !strings.Contains(detail(t, rec), "FSM compiler unavailable") {
		t.Fatal(rec.Code, rec.Body)
	}
}

func TestDeployGroupsByArchAndConfig(t *testing.T) {
	f := newFake(t)
	f.binaries = map[string][]byte{"arm64": []byte("ARM"), "amd64": []byte("AMD")}
	s := &fakeSender{}
	u := newUploads(t, lister(
		cfgVehicle("100.64.0.1.alpha", "arm64", "A"),
		cfgVehicle("100.64.0.1.bravo", "arm64", "A"),
		cfgVehicle("100.64.0.2.charlie", "amd64", "A"),
	), s, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"100.64.0.1.alpha", "100.64.0.1.bravo", "100.64.0.2.charlie"}, "m"))
	if rec.Code != 200 {
		t.Fatal(rec.Code, rec.Body)
	}
	if len(f.builds) != 2 {
		t.Fatalf("builds %d", len(f.builds))
	}
	for _, v := range []string{"100.64.0.1.alpha", "100.64.0.1.bravo"} {
		if string(s.binaryFor(t, v)) != "ARM" {
			t.Fatalf("%s got %q", v, s.binaryFor(t, v))
		}
	}
	if string(s.binaryFor(t, "100.64.0.2.charlie")) != "AMD" {
		t.Fatal("charlie")
	}
	evs := events(t, rec)
	if evs[0]["stage"] != "building" {
		t.Fatalf("first event %v", evs[0])
	}
}

func TestDeploySameArchDifferentConfigBuildsTwice(t *testing.T) {
	f := newFake(t)
	u := newUploads(t, lister(cfgVehicle("100.64.0.1.a", "arm64", "A"), cfgVehicle("100.64.0.1.b", "arm64", "B")), &fakeSender{}, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"100.64.0.1.a", "100.64.0.1.b"}, "m"))
	if len(f.builds) != 2 {
		t.Fatalf("builds %d", len(f.builds))
	}
}

func TestDeployBuildsThenUploads(t *testing.T) {
	s := &fakeSender{}
	u := newUploads(t, lister(cfgVehicle("100.64.0.1.a", "amd64", "")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"100.64.0.1.a"}, "m"))
	var types []string
	for _, e := range events(t, rec) {
		if e["type"] == "stage" {
			types = append(types, e["stage"].(string))
		}
	}
	if !reflect.DeepEqual(types, []string{"building", "uploading"}) {
		t.Fatal(types)
	}
	if c := s.called(); len(c) != 1 || c[0].name != "m" {
		t.Fatal(c)
	}
}

func TestDeployDriverErrFailsOnlyThatVehicle(t *testing.T) {
	bad := cfgVehicle("100.64.0.1.bad", "amd64", "A")
	bad.DriverErr = errors.New("plugin config has no driver cap")
	s := &fakeSender{}
	u := newUploads(t, lister(bad, cfgVehicle("100.64.0.1.ok", "amd64", "A")), s, nil)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"100.64.0.1.bad", "100.64.0.1.ok"}, "m"))
	res := results(events(t, rec))
	if res["100.64.0.1.bad"]["details"] != "driver config: plugin config has no driver cap" || res["100.64.0.1.bad"]["success"] != false {
		t.Fatal(res)
	}
	if res["100.64.0.1.ok"]["success"] != true {
		t.Fatal(res)
	}
	if c := s.called(); len(c) != 1 || c[0].vehicle != "100.64.0.1.ok" {
		t.Fatal(c)
	}
}

func TestDeployAllFailResolutionSkipsStages(t *testing.T) {
	u := newUploads(t, lister(), &fakeSender{}, nil)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"ghost"}, "m"))
	evs := events(t, rec)
	if len(evs) != 1 || evs[0]["type"] != "result" || evs[0]["details"] != "not found" {
		t.Fatal(evs)
	}
}

func TestDeployCompileErrorsStopBeforeUpload(t *testing.T) {
	f := newFake(t)
	s := &fakeSender{}
	u := newUploads(t, lister(cfgVehicle("100.64.0.1.a", "amd64", "")), s, f)
	rec := httptest.NewRecorder()
	// A graph without a start is rejected structurally.
	body := graphBody(t, func(m map[string]any) {
		m["vehicles"] = []string{"100.64.0.1.a"}
		m["name"] = "m"
		m["start_id"] = nil
	})
	u.ServeDeploy(rec, httptest.NewRequest(http.MethodPost, "/api/deploy", bytes.NewReader(body)))
	evs := events(t, rec)
	last := evs[len(evs)-1]
	if last["type"] != "error" || !strings.HasPrefix(last["detail"].(string), "Build failed: ") || len(last["errors"].([]any)) == 0 {
		t.Fatal(evs)
	}
	if len(s.called()) != 0 {
		t.Fatal("sender was called")
	}
}

func TestDeployStructuralErrorsComeFirst(t *testing.T) {
	f := newFake(t)
	s := &fakeSender{}
	u := newUploads(t, lister(cfgVehicle("100.64.0.1.a", "amd64", "")), s, f)
	rec := httptest.NewRecorder()
	body := graphBody(t, func(m map[string]any) {
		m["vehicles"] = []string{"ghost", "100.64.0.1.a"}
		m["name"] = "m"
		m["start_id"] = nil
	})
	u.ServeDeploy(rec, httptest.NewRequest(http.MethodPost, "/api/deploy", bytes.NewReader(body)))
	evs := events(t, rec)
	if len(evs) != 1 || evs[0]["type"] != "error" || !strings.HasPrefix(evs[0]["detail"].(string), "Build failed: ") || len(evs[0]["errors"].([]any)) == 0 {
		t.Fatal(evs)
	}
	if len(s.called()) != 0 {
		t.Fatal("sender was called")
	}
}

func TestDeployGroupFailureUploadsNothing(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(req BuildRequest) error {
		if req.Arch == "arm64" {
			return &CompileErrors{Errors: []DeclError{{Message: "boom"}}}
		}
		return nil
	}
	s := &fakeSender{}
	u := newUploads(t, lister(cfgVehicle("100.64.0.1.a", "arm64", "A"), cfgVehicle("100.64.0.1.b", "amd64", "A")), s, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"100.64.0.1.a", "100.64.0.1.b"}, "m"))
	evs := events(t, rec)
	last := evs[len(evs)-1]
	if last["type"] != "error" {
		t.Fatal(evs)
	}
	errs := last["errors"].([]any)
	if msg := errs[0].(map[string]any)["message"].(string); !strings.HasPrefix(msg, "100.64.0.1.a: ") {
		t.Fatalf("message %q", msg)
	}
	if len(s.called()) != 0 {
		t.Fatal("sender was called")
	}
}

func TestDeployCompilerFailureIsTerminalError(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(BuildRequest) error { return errors.New("go build exploded") }
	s := &fakeSender{}
	u := newUploads(t, lister(cfgVehicle("100.64.0.1.a", "amd64", "")), s, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"100.64.0.1.a"}, "m"))
	evs := events(t, rec)
	last := evs[len(evs)-1]
	if last["type"] != "error" || last["detail"] != "Build failed: go build exploded" {
		t.Fatal(evs)
	}
	if _, has := last["errors"]; has {
		t.Fatal("unexpected errors field")
	}
	if len(s.called()) != 0 {
		t.Fatal("sender was called")
	}
}

// --- deploy with flows ---

// engineFake is a fake compiler whose registry also has actions.Track, with
// a DetectionEngine field.
func engineFake(t testing.TB) *fakeCompiler {
	t.Helper()
	f := newFake(t)
	f.registry["actions"].(map[string]any)["actions.Track"] = map[string]any{
		"type":   "actions.Track",
		"fields": []any{map[string]any{"name": "Engine", "type": "params.DetectionEngine"}},
	}
	return f
}

// flowCatalog lists alpha and bravo on 100.64.0.1 and charlie on
// 100.64.0.2 (host swap03), each with telemetry only; the daemons with yoloIPs run yolo.
func flowCatalog(yoloIPs ...string) flows.Catalog {
	telemetry := []flows.Stream{{Name: "telemetry", Kind: "telemetry"}}
	cat := flows.Catalog{Daemons: []flows.Daemon{
		{IP: "100.64.0.1", Local: true, Vehicles: []flows.Vehicle{
			{Name: "100.64.0.1.alpha", Streams: telemetry, Typed: true},
			{Name: "100.64.0.1.bravo", Streams: telemetry, Typed: true},
		}},
		{IP: "100.64.0.2", Hostname: "swap03", Vehicles: []flows.Vehicle{{Name: "100.64.0.2.charlie", Streams: telemetry, Typed: true}}},
	}}
	for _, ip := range yoloIPs {
		for i := range cat.Daemons {
			if cat.Daemons[i].IP == ip {
				cat.Daemons[i].Engines = append(cat.Daemons[i].Engines, flows.Engine{
					Name: ip + ".yolo", Bare: "yolo", Result: "messages/result/DetectionResult", Typed: true,
				})
			}
		}
	}
	return cat
}

// localYolo is a flow designed for alpha: its telemetry into the yolo on the
// orchestrator's daemon, 100.64.0.1.
func localYolo() *flows.Template {
	return &flows.Template{
		Vehicle:   "100.64.0.1.alpha",
		Producers: []flows.TemplateProducer{{Name: "100.64.0.1.telemetry"}},
		Engines:   []flows.TemplateEngine{{Name: "100.64.0.1.yolo"}},
		Edges:     []flows.TemplateEdge{{From: "100.64.0.1.telemetry", To: "100.64.0.1.yolo", Timeout: "1s"}},
	}
}

// remoteDet is a flow with an engine the catalog doesn't have.
func remoteDet() *flows.Template {
	return &flows.Template{
		Vehicle:   "100.64.0.1.alpha",
		Producers: []flows.TemplateProducer{{Name: "100.64.0.1.telemetry"}},
		Engines:   []flows.TemplateEngine{{Name: "100.64.0.1.det"}},
		Edges:     []flows.TemplateEdge{{From: "100.64.0.1.telemetry", To: "100.64.0.1.det", Timeout: "1s"}},
	}
}

// trackBody is a one-node mission, a Track with the engine, with tpl as its
// flow (none if nil); mutate, if set, edits the body.
func trackBody(t testing.TB, engine string, tpl *flows.Template, mutate func(map[string]any)) []byte {
	t.Helper()
	m := map[string]any{
		"nodes":    []any{map[string]any{"instance_id": "trk", "type_name": "Track", "params": map[string]any{"Engine": engine}}},
		"events":   []any{},
		"edges":    []any{},
		"start_id": "trk",
	}
	if tpl != nil {
		m["flow"] = tpl
	}
	if mutate != nil {
		mutate(m)
	}
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func trackDeployReq(t testing.TB, engine string, tpl *flows.Template, vehicles ...string) *http.Request {
	t.Helper()
	body := trackBody(t, engine, tpl, func(m map[string]any) {
		m["vehicles"] = vehicles
		m["name"] = "m"
	})
	return httptest.NewRequest(http.MethodPost, "/api/deploy", bytes.NewReader(body))
}

// manifestEngines parses a manifest and returns its engines' names.
func manifestEngines(t testing.TB, data []byte) []string {
	t.Helper()
	m, err := config.ParseMissionManifestFromBytes(data)
	if err != nil {
		t.Fatalf("manifest %q: %v", data, err)
	}
	var names []string
	for _, e := range m.Engines {
		names = append(names, e.Name)
	}
	return names
}

func flowFleet() []fleet.Vehicle {
	return []fleet.Vehicle{
		cfgVehicle("100.64.0.1.alpha", "arm64", "A"),
		cfgVehicle("100.64.0.1.bravo", "arm64", "A"),
		cfgVehicle("100.64.0.2.charlie", "arm64", "A"),
	}
}

// charlieYolo is a flow designed for charlie: its telemetry into the yolo on
// its own daemon.
func charlieYolo(host string) *flows.Template {
	return &flows.Template{
		Vehicle:   "100.64.0.2.charlie",
		Producers: []flows.TemplateProducer{{Name: host + ".telemetry"}},
		Engines:   []flows.TemplateEngine{{Name: host + ".yolo"}},
		Edges:     []flows.TemplateEdge{{From: host + ".telemetry", To: host + ".yolo", Timeout: "1s"}},
	}
}

func TestDeployFlowLocalPerTarget(t *testing.T) {
	f := engineFake(t)
	s := &fakeSender{}
	u := newFlowUploads(t, catalogLister(flowCatalog("100.64.0.1", "100.64.0.2"), flowFleet()...), s, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, trackDeployReq(t, "100.64.0.2.yolo", charlieYolo("100.64.0.2"), "100.64.0.1.alpha", "100.64.0.1.bravo", "100.64.0.2.charlie"))
	res := results(events(t, rec))
	if c := res["100.64.0.2.charlie"]; c["success"] != true {
		t.Fatalf("charlie %v", c)
	}
	// Producers re-home to each target, so it is the engine on charlie's
	// daemon that keeps the other vehicles from the flow.
	for _, v := range []string{"100.64.0.1.alpha", "100.64.0.1.bravo"} {
		if r := res[v]; r["success"] != false || r["details"] != "flow engine 100.64.0.2.yolo is neither on this vehicle's daemon nor on the orchestrator" {
			t.Errorf("%s: %v", v, r)
		}
	}
	if len(f.builds) != 1 || len(s.called()) != 1 {
		t.Fatalf("builds %d sends %d", len(f.builds), len(s.called()))
	}
	b := f.builds[0]
	if !strings.Contains(string(b.Flow), "100.64.0.2.yolo") || !strings.Contains(string(b.Flow), `location = "local"`) {
		t.Errorf("flow %s", b.Flow)
	}
	if got := manifestEngines(t, b.Manifest); !reflect.DeepEqual(got, []string{"100.64.0.2.yolo"}) {
		t.Errorf("manifest engines %v", got)
	}
	eq(t, engineIdent(t, b.Ast), "DetectionEngine_100_64_0_2_yolo")
}

func TestDeployFlowEngineOnOtherDaemonFailsTarget(t *testing.T) {
	f := engineFake(t)
	s := &fakeSender{}
	u := newFlowUploads(t, catalogLister(flowCatalog("100.64.0.1", "100.64.0.2"), flowFleet()...), s, f)
	tpl := &flows.Template{Engines: []flows.TemplateEngine{{Name: "100.64.0.2.yolo"}}}
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, trackDeployReq(t, "100.64.0.2.yolo", tpl, "100.64.0.1.alpha", "100.64.0.2.charlie"))
	res := results(events(t, rec))
	if a := res["100.64.0.1.alpha"]; a["success"] != false || a["details"] != "flow engine 100.64.0.2.yolo is neither on this vehicle's daemon nor on the orchestrator" {
		t.Errorf("alpha %v", a)
	}
	if c := res["100.64.0.2.charlie"]; c["success"] != true {
		t.Errorf("charlie %v", c)
	}
	if len(f.builds) != 1 {
		t.Fatalf("builds %d", len(f.builds))
	}
}

func TestDeployFlowOrchestratorEngineEverywhere(t *testing.T) {
	f := engineFake(t)
	s := &fakeSender{}
	u := newFlowUploads(t, catalogLister(flowCatalog("100.64.0.1", "100.64.0.2"), flowFleet()...), s, f)
	tpl := &flows.Template{Engines: []flows.TemplateEngine{{Name: "100.64.0.1.yolo"}}}
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, trackDeployReq(t, "100.64.0.1.yolo", tpl, "100.64.0.1.alpha", "100.64.0.2.charlie"))
	res := results(events(t, rec))
	for _, v := range []string{"100.64.0.1.alpha", "100.64.0.2.charlie"} {
		if res[v]["success"] != true {
			t.Errorf("%s: %v", v, res[v])
		}
	}
	// On the orchestrator's own daemon the engine is local, elsewhere
	// remote, so the two located flows build separately.
	if len(f.builds) != 2 {
		t.Fatalf("builds %d: %v", len(f.builds), events(t, rec))
	}
	locals := 0
	for _, b := range f.builds {
		if strings.Contains(string(b.Flow), `location = "local"`) {
			locals++
		} else if !strings.Contains(string(b.Flow), `location = "remote"`) {
			t.Errorf("flow %s", b.Flow)
		}
		eq(t, engineIdent(t, b.Ast), "DetectionEngine_100_64_0_1_yolo")
	}
	if locals != 1 {
		t.Errorf("%d local flows, want 1", locals)
	}
}

func TestDeployFlowUnknownHostFailsDeploy(t *testing.T) {
	f := engineFake(t)
	s := &fakeSender{}
	u := newFlowUploads(t, catalogLister(flowCatalog("100.64.0.1"), flowFleet()...), s, f)
	tpl := &flows.Template{Engines: []flows.TemplateEngine{{Name: "heisenberg.yolo"}}}
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, trackDeployReq(t, "heisenberg.yolo", tpl, "100.64.0.1.alpha"))
	evs := events(t, rec)
	if len(evs) != 1 || evs[0]["type"] != "error" || !strings.Contains(evs[0]["detail"].(string), "flow host heisenberg is not a known daemon") {
		t.Fatal(evs)
	}
	if len(f.builds) != 0 || len(s.called()) != 0 {
		t.Fatal("compiled or sent")
	}
}

func TestDeployFlowProducerRehomesToTarget(t *testing.T) {
	f := engineFake(t)
	s := &fakeSender{}
	u := newFlowUploads(t, catalogLister(flowCatalog("100.64.0.1", "100.64.0.2"), flowFleet()...), s, f)
	// Designed on charlie's daemon, but each target uses its own telemetry.
	tpl := charlieYolo("100.64.0.2")
	tpl.Engines = []flows.TemplateEngine{{Name: "100.64.0.1.yolo"}}
	tpl.Edges = []flows.TemplateEdge{{From: "100.64.0.2.telemetry", To: "100.64.0.1.yolo", Timeout: "1s"}}
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, trackDeployReq(t, "100.64.0.1.yolo", tpl, "100.64.0.1.alpha", "100.64.0.2.charlie"))
	res := results(events(t, rec))
	for _, v := range []string{"100.64.0.1.alpha", "100.64.0.2.charlie"} {
		if res[v]["success"] != true {
			t.Errorf("%s: %v", v, res[v])
		}
	}
	for _, b := range f.builds {
		if !strings.Contains(string(b.Flow), `name = "telemetry"`) {
			t.Errorf("flow %s", b.Flow)
		}
	}
}

func TestDeployEngineReferenceWithoutFlow(t *testing.T) {
	f := engineFake(t)
	s := &fakeSender{}
	u := newFlowUploads(t, catalogLister(flowCatalog("100.64.0.1"), flowFleet()...), s, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, trackDeployReq(t, "yolo", nil, "100.64.0.1.alpha"))
	evs := events(t, rec)
	if len(evs) != 1 || evs[0]["type"] != "error" || !strings.Contains(evs[0]["detail"].(string), "mission uses engine yolo in") {
		t.Fatal(evs)
	}
	if len(f.builds) != 0 || len(s.called()) != 0 {
		t.Fatal("compiled or sent")
	}
}

func TestDeployWithoutFlowUnchanged(t *testing.T) {
	f := newFake(t)
	f.binaries = map[string][]byte{"arm64": []byte("ARM"), "amd64": []byte("AMD")}
	s := &fakeSender{}
	u := newUploads(t, lister(
		cfgVehicle("100.64.0.1.alpha", "arm64", "A"),
		cfgVehicle("100.64.0.1.bravo", "arm64", "A"),
		cfgVehicle("100.64.0.2.charlie", "amd64", "A"),
	), s, f)
	rec := httptest.NewRecorder()
	u.ServeDeploy(rec, deployReq(t, []string{"100.64.0.1.alpha", "100.64.0.1.bravo", "100.64.0.2.charlie"}, "m"))
	if len(f.builds) != 2 {
		t.Fatalf("builds %d", len(f.builds))
	}
	for _, b := range f.builds {
		if b.Flow != nil || b.Manifest != nil {
			t.Errorf("build has flow %q manifest %q", b.Flow, b.Manifest)
		}
	}
	evs := events(t, rec)
	if evs[0]["stage"] != "building" {
		t.Fatalf("first event %v", evs[0])
	}
	res := results(evs)
	for v, want := range map[string]string{"100.64.0.1.alpha": "ARM", "100.64.0.1.bravo": "ARM", "100.64.0.2.charlie": "AMD"} {
		if res[v]["success"] != true || string(s.binaryFor(t, v)) != want {
			t.Errorf("%s: %v %q", v, res[v], s.binaryFor(t, v))
		}
	}
}
