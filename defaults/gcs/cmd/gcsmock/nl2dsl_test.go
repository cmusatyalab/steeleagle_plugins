//go:build linux

package main

import (
	"bytes"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
)

func TestE2ENL2DSLSDK13(t *testing.T) {
	if os.Getenv("GCS_COMPILER_E2E") != "1" {
		t.Skip("set GCS_COMPILER_E2E=1 (needs Go SDK modules and go build)")
	}
	if testing.Short() {
		t.Skip("end-to-end test skipped in -short mode")
	}
	clip := filepath.Join(t.TempDir(), "fixture.mp4")
	cmd := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10:d=2",
		"-c:v", "libx264", "-threads", "1", clip)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("create test clip: %v: %s", err, output)
	}

	const trackInstruction = "Track the person, then return home. If the battery drops to 30 percent, return home."
	for _, tc := range []struct {
		name, instruction  string
		withFlow, fallback bool
		wantDSL            []string
	}{
		{"Track with detector flow", trackInstruction, true, false,
			[]string{"Camera=params.Camera_front", `Class="person"`, "Threshold=30", "done -> home", "low_battery -> home"}},
		{"Track without detector flow", trackInstruction, false, true,
			[]string{"actions.Wait waiting(Duration=30", "done -> home"}},
		{"Unclear activity uses fallback", "Wait 30 seconds, then return home.", false, true,
			[]string{"actions.Wait waiting(Duration=30", "done -> home"}},
		{"Climb, wait, return home", "Climb to 5 meters, then wait 30 seconds, then return home.", false, false,
			[]string{"gcsactions.ClimbToAltitude climb(Altitude=5", "actions.Wait waiting(Duration=30", "done -> waiting", "done -> home"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			const key = "mock-gcs-test-key"
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				if req.URL.Path != "/responses" || req.Header.Get("Authorization") != "Bearer "+key {
					http.Error(w, "unexpected model request", http.StatusBadRequest)
					return
				}
				body, err := io.ReadAll(req.Body)
				if err != nil {
					http.Error(w, err.Error(), http.StatusBadRequest)
					return
				}
				if !bytes.Contains(body, []byte(`"input_image"`)) || !bytes.Contains(body, []byte("mission inference V6")) {
					t.Error("expected only a V6 vision request, followed by deterministic compilation")
					http.Error(w, "unexpected model request", http.StatusBadRequest)
					return
				}
				reply := map[string]string{"type": "output_text", "text": tc.instruction}
				_ = json.NewEncoder(w).Encode(map[string]any{"output": []any{reply}})
			}))
			defer model.Close()
			t.Setenv("XDG_CONFIG_HOME", t.TempDir())
			t.Setenv("OPENAI_API_KEY", key)
			t.Setenv("OPENAI_BASE_URL", model.URL)
			t.Setenv("STEELEAGLE_VISION_BASE_URL", model.URL)
			t.Setenv("STEELEAGLE_INFERRED_MISSION_DATA", t.TempDir())
			r := startMock(t, false)
			r.waitFor(30*time.Second, "mission compiler readiness", func() (bool, string) {
				var status struct {
					Ready bool `json:"compiler_available"`
				}
				if err := r.getJSON("/api/inferred-mission/status", &status); err != nil {
					return false, err.Error()
				}
				return status.Ready, "compiler registry loading"
			})

			plan := mission.PlanningContext{Vehicle: r.alpha}
			detector := r.gcsIP + ".yolo"
			if tc.withFlow {
				camera, encoder := r.alpha+".front", r.gcsIP+".h264_encoder"
				plan.Flow = &flows.Template{
					Vehicle:   r.alpha,
					Producers: []flows.TemplateProducer{{Name: camera}},
					Engines:   []flows.TemplateEngine{{Name: detector}, {Name: encoder}},
					Edges:     []flows.TemplateEdge{{From: camera, To: detector, Timeout: "2s"}, {From: detector, To: encoder, Timeout: "2s"}},
				}
			}
			var upload bytes.Buffer
			form := multipart.NewWriter(&upload)
			part, err := form.CreateFormFile("file", filepath.Base(clip))
			if err != nil {
				t.Fatal(err)
			}
			data, err := os.ReadFile(clip)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := part.Write(data); err != nil {
				t.Fatal(err)
			}
			if err := form.Close(); err != nil {
				t.Fatal(err)
			}
			var asset, session struct{ ID string }
			postNL2DSLJSON(t, r.url+"/api/inferred-mission/videos", form.FormDataContentType(), &upload, http.StatusCreated, &asset)
			body, err := json.Marshal(map[string]any{"source_type": "uploaded_video", "video_asset_id": asset.ID, "planning_context": plan})
			if err != nil {
				t.Fatal(err)
			}
			postNL2DSLJSON(t, r.url+"/api/inferred-mission/sessions", "application/json", bytes.NewReader(body), http.StatusCreated, &session)

			var row struct {
				Status, Error, DSL, Adaptation string
				CompileStatus                  string `json:"compile_status"`
				Attempts                       []struct{ Errors []string }
				Mission                        struct{ Graph mission.Graph } `json:"mission_json"`
			}
			r.waitFor(90*time.Second, "NL2DSL inference", func() (bool, string) {
				if err := r.getJSON("/api/inferred-mission/sessions/"+session.ID+"/sections/1", &row); err != nil {
					return false, err.Error()
				}
				return row.Status == "failed" || row.Status == "completed", row.Status
			})
			if row.Status != "completed" || row.CompileStatus != "compiled" {
				t.Fatalf("inference %s, compile %s, want completed/compiled: %s, diagnostics: %v", row.Status, row.CompileStatus, row.Error, row.Attempts)
			}
			for _, want := range tc.wantDSL {
				if !strings.Contains(row.DSL, want) {
					t.Errorf("missing %q from generated DSL:\n%s", want, row.DSL)
				}
			}
			if tc.withFlow && (!strings.Contains(row.DSL, `Engine="`+detector+`"`) || strings.Contains(row.DSL, "UNBOUND")) {
				t.Errorf("Track did not bind the configured camera and detector:\n%s", row.DSL)
			}
			if tc.fallback && (!strings.Contains(strings.ToLower(row.Adaptation), "fallback policy") || strings.Contains(row.DSL, "actions.Track")) {
				t.Errorf("expected a documented policy fallback, got %q:\n%s", row.Adaptation, row.DSL)
			}
			request := struct {
				mission.Graph
				Vehicle string          `json:"vehicle"`
				Flow    *flows.Template `json:"flow,omitempty"`
			}{row.Mission.Graph, plan.Vehicle, plan.Flow}
			body, err = json.Marshal(request)
			if err != nil {
				t.Fatal(err)
			}
			var rendered struct{ DSL string }
			postNL2DSLJSON(t, r.url+"/api/render_dsl", "application/json", bytes.NewReader(body), http.StatusOK, &rendered)
			if !strings.Contains(rendered.DSL, `"github.com/cmusatyalab/steeleagle/sdk" v0.1.3`) {
				t.Errorf("GCS did not select SDK v0.1.3:\n%s", rendered.DSL)
			}
			if tc.withFlow && !strings.Contains(rendered.DSL, "Engine=DetectionEngine_local_yolo") {
				t.Errorf("export did not use the SDK's relative detector constant:\n%s", rendered.DSL)
			}
			t.Logf("SDK v0.1.3 draft status: %s", row.CompileStatus)
		})
	}

	t.Run("Patrol timer and named area", func(t *testing.T) {
		t.Setenv("XDG_CONFIG_HOME", t.TempDir())
		r := startMock(t, false)
		const patrol = `Actions:
    actions.TakeOff takeoff(Altitude=20.0)
    actions.Patrol patrol(Area=params.Polygon_UNBOUND_areaB, Altitude=20.0, AltitudeMode=enums.AltitudeModeRelative)
    actions.ReturnToHome home()
Events:
    events.TimeElapsed patrol_timeout(Duration=50.0)
Mission:
    Start takeoff
    During takeoff:
        done -> patrol
    During patrol:
        patrol_timeout -> home
        done -> home
`
		const geojson = `{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"areaB"},"geometry":{"type":"Polygon","coordinates":[[[-79.94,40.44],[-79.939,40.44],[-79.939,40.441],[-79.94,40.44]]]}}]}`
		for _, tc := range []struct {
			dsl, geojson string
			wantOK       bool
		}{
			{patrol, "", false},
			{strings.Replace(patrol, "Polygon_UNBOUND_areaB", "Polygon_areaB", 1), geojson, true},
		} {
			body, _ := json.Marshal(map[string]any{"dsl": tc.dsl, "planning_context": mission.PlanningContext{Vehicle: r.alpha, GeoJSON: tc.geojson}})
			var parsed struct {
				mission.Graph
				BindingErrors []mission.GraphError `json:"binding_errors"`
			}
			postNL2DSLJSON(t, r.url+"/api/parse_dsl", "application/json", bytes.NewReader(body), http.StatusOK, &parsed)
			if tc.wantOK != (len(parsed.BindingErrors) == 0) {
				t.Errorf("Patrol import binding issues: %+v", parsed.BindingErrors)
			}
			parsed.Graph.GeoJSON = tc.geojson
			body, _ = json.Marshal(struct {
				mission.Graph
				Vehicle string `json:"vehicle"`
			}{parsed.Graph, r.alpha})
			var result struct {
				OK     bool
				Errors []mission.GraphError
			}
			postNL2DSLJSON(t, r.url+"/api/compile", "application/json", bytes.NewReader(body), http.StatusOK, &result)
			if tc.wantOK && !result.OK {
				t.Fatalf("Patrol with named areaB and 50-second timer did not compile: %+v", result.Errors)
			}
			if !tc.wantOK && (result.OK || len(result.Errors) != 1 || result.Errors[0].NodeID == nil ||
				*result.Errors[0].NodeID != "patrol" || !strings.Contains(strings.ToLower(result.Errors[0].Message), "area")) {
				t.Fatalf("unbound Patrol must identify its missing area: %+v", result.Errors)
			}
		}
	})
}

func postNL2DSLJSON(t *testing.T, url, contentType string, body io.Reader, wantStatus int, out any) {
	t.Helper()
	resp, err := http.Post(url, contentType, body)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != wantStatus {
		detail, _ := io.ReadAll(resp.Body)
		t.Fatalf("POST %s = %d, want %d: %s", url, resp.StatusCode, wantStatus, detail)
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		t.Fatal(err)
	}
}
