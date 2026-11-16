package pins

import (
	"encoding/json"
	"mime"
	"net/http"
	"time"
)

// maxSettingsBody bounds a settings request's body.
const maxSettingsBody = 1024

// Routes serves the pins and their settings:
//
//   - GET /api/pins: {"settings": Settings, "pins": [Pin...]}
//   - PUT /api/pins/settings: replaces the settings, answering them
//   - POST /api/pins/clear: removes every pin, answering {"cleared": N}
//
// The changes need a JSON content type, which a browser only sends to
// another site after a CORS preflight, so other sites can't make them.
func Routes(mux *http.ServeMux, store *Store) {
	mux.HandleFunc("GET /api/pins", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, pinsResponse{Settings: store.Settings(), Pins: store.Snapshot(time.Now())})
	})
	mux.HandleFunc("PUT /api/pins/settings", func(w http.ResponseWriter, r *http.Request) {
		if !isJSON(r) {
			writeJSON(w, http.StatusUnsupportedMediaType, map[string]string{"detail": "content type must be application/json"})
			return
		}
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxSettingsBody))
		dec.DisallowUnknownFields()
		var s Settings
		if err := dec.Decode(&s); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "invalid settings: " + err.Error()})
			return
		}
		if err := store.SetSettings(s); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, store.Settings())
	})
	mux.HandleFunc("POST /api/pins/clear", func(w http.ResponseWriter, r *http.Request) {
		if !isJSON(r) {
			writeJSON(w, http.StatusUnsupportedMediaType, map[string]string{"detail": "content type must be application/json"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]int{"cleared": store.Clear()})
	})
}

// pinsResponse is the answer to GET /api/pins.
type pinsResponse struct {
	Settings Settings `json:"settings"`
	Pins     []Pin    `json:"pins"`
}

func isJSON(r *http.Request) bool {
	t, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	return err == nil && t == "application/json"
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
