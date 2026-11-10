package metrics

import (
	"context"
	"encoding/json"
	"net/http"
	"slices"
	"time"

	"github.com/rs/zerolog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// QueryTimeout bounds asking Prometheus for one view's numbers, so a hung
// or overloaded server reads as unreachable before the page's next poll
// (every 5 s).
const QueryTimeout = 3 * time.Second

// FlowFetcher reads the TOML of the flow a vehicle runs, empty if it runs
// none.
type FlowFetcher func(ctx context.Context, vehicle string) ([]byte, error)

// NewFlowHandler serves GET /api/metrics/flow?vehicle=&window=: the
// vehicle's running flow with its numbers over the window (DefaultWindow if
// unset), as a View. It answers 400 for a missing vehicle or a window not in
// Windows, 404 for a vehicle the daemon doesn't know, and 502 if the flow
// can't be read. Otherwise it answers 200, with the state saying whether
// the vehicle runs no flow, or Prometheus couldn't be asked, in which case
// the flow comes without numbers.
func NewFlowHandler(flows FlowFetcher, d DaemonConfigs, q Querier, log zerolog.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		vehicle := r.URL.Query().Get("vehicle")
		window := r.URL.Query().Get("window")
		if window == "" {
			window = DefaultWindow
		}
		if vehicle == "" {
			writeDetail(w, http.StatusBadRequest, "vehicle is required")
			return
		}
		if !slices.Contains(Windows, window) {
			writeDetail(w, http.StatusBadRequest, "window must be one of 1m, 5m, 15m, 1h")
			return
		}

		flowTOML, err := flows(r.Context(), vehicle)
		if err != nil {
			if status.Code(err) == codes.NotFound {
				writeDetail(w, http.StatusNotFound, status.Convert(err).Message())
				return
			}
			log.Warn().Err(err).Str("vehicle", vehicle).Msg("could not read the vehicle's flow for its metrics")
			writeDetail(w, http.StatusBadGateway, status.Convert(err).Message())
			return
		}
		view, err := BuildView(flowTOML, vehicle, hostnames(r.Context(), d))
		if err != nil {
			writeDetail(w, http.StatusBadGateway, "the vehicle's flow can't be parsed: "+err.Error())
			return
		}
		view.Window = window
		switch {
		case len(view.Nodes) == 0:
			view.State = StateNoFlow
		default:
			ctx, cancel := context.WithTimeout(r.Context(), QueryTimeout)
			err := Fill(ctx, q, &view, vehicle, window)
			cancel()
			if err != nil {
				log.Warn().Err(err).Msg("could not query Prometheus")
				view.State = StatePrometheusUnreachable
				view.Detail = err.Error()
				break
			}
			MarkBottlenecks(&view)
			view.State = StateOK
		}
		writeJSON(w, http.StatusOK, view)
	}
}

// hostnames maps each daemon's IP to its hostname, or is empty if the
// daemons can't be listed.
func hostnames(ctx context.Context, d DaemonConfigs) map[string]string {
	listed, err := d.List(ctx)
	if err != nil {
		return map[string]string{}
	}
	out := make(map[string]string, len(listed))
	for _, daemon := range listed {
		out[daemon.IP] = daemon.Hostname
	}
	return out
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeDetail(w http.ResponseWriter, code int, detail string) {
	writeJSON(w, code, map[string]string{"detail": detail})
}
