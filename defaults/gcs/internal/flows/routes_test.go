package flows

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

const exampleFlowTOML = `[[producers]]
name = "swap03.telemetry"
tokens = 2

[[engines]]
name = "swap03.yolo"
location = "local"

[[edges]]
from = "swap03.telemetry"
to = "swap03.yolo"
timeout = "2s"
`

func fromTOML(t *testing.T, doc string) *httptest.ResponseRecorder {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"toml": doc})
	rec := httptest.NewRecorder()
	ServeFromTOML(rec, httptest.NewRequest(http.MethodPost, "/api/flows/from_toml", strings.NewReader(string(body))))
	return rec
}

func TestFromTomlRoute(t *testing.T) {
	rec := fromTOML(t, exampleFlowTOML)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	var got Template
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	want := Template{
		Producers: []TemplateProducer{{Name: "swap03.telemetry", Tokens: intp(2)}},
		Engines:   []TemplateEngine{{Name: "swap03.yolo"}},
		Edges:     []TemplateEdge{{From: "swap03.telemetry", To: "swap03.yolo", Timeout: "2s"}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %+v, want %+v", got, want)
	}
}

func TestFromTomlRouteRejects(t *testing.T) {
	for name, tc := range map[string]struct{ doc, detail string }{
		"unknown table": {"[[nope]]", "unknown flow keys"},
		"old format":    {"[gabriel.flow]\nname = \"x\"\n", "unknown flow keys"},
		"bad toml":      {"[[producers", ""},
		"empty":         {"", "no producers or engines in this flow"},
	} {
		t.Run(name, func(t *testing.T) {
			rec := fromTOML(t, tc.doc)
			if rec.Code != http.StatusUnprocessableEntity {
				t.Fatalf("status %d: %s", rec.Code, rec.Body)
			}
			var d struct{ Detail string }
			if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil || d.Detail == "" || !strings.Contains(d.Detail, tc.detail) {
				t.Errorf("body %s, want detail containing %q", rec.Body, tc.detail)
			}
		})
	}
}

func TestFromTomlRouteBadRequest(t *testing.T) {
	rec := httptest.NewRecorder()
	ServeFromTOML(rec, httptest.NewRequest(http.MethodPost, "/api/flows/from_toml", strings.NewReader("not json")))
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status %d", rec.Code)
	}
}
