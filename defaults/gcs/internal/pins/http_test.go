package pins

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func serve(store *Store, method, path, contentType, body string) *httptest.ResponseRecorder {
	mux := http.NewServeMux()
	Routes(mux, store)
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)
	return w
}

func TestGetPinsEmpty(t *testing.T) {
	w := serve(NewStore(), "GET", "/api/pins", "", "")
	want := `{"settings":{"radius_m":5,"expiry_s":60,"min_sightings":3},"pins":[]}`
	if w.Code != 200 || strings.TrimSpace(w.Body.String()) != want {
		t.Fatalf("got %d %s, want %s", w.Code, w.Body, want)
	}
}

func TestPutSettings(t *testing.T) {
	store := NewStore()
	w := serve(store, "PUT", "/api/pins/settings", "application/json", `{"radius_m":10,"expiry_s":300,"min_sightings":2}`)
	if w.Code != 200 {
		t.Fatalf("got %d %s", w.Code, w.Body)
	}
	if got := store.Settings(); got != (Settings{RadiusM: 10, ExpiryS: 300, MinSightings: 2}) {
		t.Fatalf("settings %+v", got)
	}
	w = serve(store, "GET", "/api/pins", "", "")
	if !strings.Contains(w.Body.String(), `"radius_m":10`) {
		t.Fatalf("GET after PUT: %s", w.Body)
	}
}

func TestPutSettingsRejects(t *testing.T) {
	store := NewStore()
	for _, tt := range []struct {
		name, contentType, body string
		code                    int
	}{
		{"out of range", "application/json", `{"radius_m":25,"expiry_s":60,"min_sightings":3}`, 400},
		{"not json type", "text/plain", `{"radius_m":10,"expiry_s":60,"min_sightings":3}`, 415},
		{"form type", "application/x-www-form-urlencoded", `radius_m=10`, 415},
		{"garbage", "application/json", `{"radius_m":`, 400},
		{"unknown field", "application/json", `{"radius_m":10,"expiry_s":60,"min_sightings":3,"x":1}`, 400},
	} {
		w := serve(store, "PUT", "/api/pins/settings", tt.contentType, tt.body)
		if w.Code != tt.code {
			t.Errorf("%s: got %d %s, want %d", tt.name, w.Code, w.Body, tt.code)
		}
		if store.Settings() != DefaultSettings {
			t.Errorf("%s: settings changed to %+v", tt.name, store.Settings())
		}
	}
}

func TestClear(t *testing.T) {
	store := NewStore()
	now := time.Now()
	for i, m := range []float64{0, 50} {
		for seq := range 3 {
			h := hit(uint64(10*i+seq), m, 0)
			h.At = now
			store.Add(h)
		}
	}
	w := serve(store, "POST", "/api/pins/clear", "text/plain", "")
	if w.Code != 415 || len(store.Snapshot(now)) != 2 {
		t.Fatalf("clear without JSON type: %d, %d pins left", w.Code, len(store.Snapshot(now)))
	}
	w = serve(store, "POST", "/api/pins/clear", "application/json", "{}")
	if w.Code != 200 || strings.TrimSpace(w.Body.String()) != `{"cleared":2}` {
		t.Fatalf("got %d %s", w.Code, w.Body)
	}
	w = serve(store, "GET", "/api/pins", "", "")
	if !strings.Contains(w.Body.String(), `"pins":[]`) {
		t.Fatalf("pins after clear: %s", w.Body)
	}
}
