package flows

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

// maxTOMLBody bounds the body of a from_toml request.
const maxTOMLBody = 1 << 20

// ServeFromTOML parses the TOML of a flow config ({"toml": "..."}) into a
// template, which it writes as JSON with no vehicle. It needs no daemon. A
// document that isn't a flow config, or has no producers or engines, is a 422
// with the reason as its detail.
func ServeFromTOML(w http.ResponseWriter, r *http.Request) {
	var req struct {
		TOML string `json:"toml"`
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxTOMLBody))
	if err == nil {
		err = json.Unmarshal(body, &req)
	}
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "invalid request: " + err.Error()})
		return
	}
	t, err := ParseTemplateTOML([]byte(req.TOML))
	if err == nil && len(t.Producers) == 0 && len(t.Engines) == 0 {
		err = errors.New("no producers or engines in this flow")
	}
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, t)
}
