// Package presence tells the web UI who currently has it open.
//
// Every open tab holds a /ws/presence websocket for as long as it is open;
// the set of open sockets is the viewer list, so there is no heartbeat or
// expiry to tune. Each join or leave sends the full list to every viewer,
// along with that viewer's own id so the UI can mark "you" (see [Message]).
//
// Purely informational: nothing here gates or locks vehicle control.
//
// Viewers are identified by the socket's peer address, which is the real
// client only while the GCS serves the UI directly. Behind a reverse proxy
// every viewer would show up as the proxy.
package presence

import (
	"context"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"github.com/google/uuid"
	"github.com/rs/zerolog"
)

const (
	// DefaultResolveTimeout is how long a viewer's reverse-DNS lookup may
	// take before the viewer is listed by its IP instead.
	DefaultResolveTimeout = 2 * time.Second
	// writeTimeout is how long a send to one viewer may take before its
	// socket is given up on.
	writeTimeout = 5 * time.Second
)

// Viewer is one open tab.
type Viewer struct {
	ID   string `json:"id"`
	Host string `json:"host"`
	IP   string `json:"ip"`
	// ConnectedAt is when the tab connected, in Unix seconds.
	ConnectedAt float64 `json:"connected_at"`
}

// Message is what each viewer is sent on every join or leave: every viewer,
// in the order they joined, and which of them it is.
type Message struct {
	Viewers []Viewer `json:"viewers"`
	You     string   `json:"you"`
}

// viewer is an open tab and the queue of its socket's writer. The queue
// holds at most the latest message: a viewer that is behind skips straight
// to the current list.
type viewer struct {
	Viewer
	send chan Message
}

// Registry is the viewer list. Serve it at /ws/presence.
type Registry struct {
	// LookupAddr reverse-resolves an IP; it is net.DefaultResolver's by
	// default.
	LookupAddr func(ctx context.Context, ip string) ([]string, error)
	// ResolveTimeout bounds LookupAddr.
	ResolveTimeout time.Duration

	log zerolog.Logger

	mu      sync.Mutex
	viewers []*viewer
	// hosts caches the names IPs resolved to; IPs without one are looked up
	// again on their next join.
	hosts map[string]string
}

// New returns an empty registry.
func New(log zerolog.Logger) *Registry {
	return &Registry{
		LookupAddr:     net.DefaultResolver.LookupAddr,
		ResolveTimeout: DefaultResolveTimeout,
		log:            log,
		hosts:          map[string]string{},
	}
}

// ServeHTTP accepts a viewer's websocket and lists the viewer until the
// socket closes. Anything the viewer sends is ignored.
func (r *Registry) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	conn, err := websocket.Accept(w, req, nil)
	if err != nil {
		r.log.Debug().Err(err).Msg("presence websocket not accepted")
		return
	}
	defer conn.CloseNow()

	ip, _, err := net.SplitHostPort(req.RemoteAddr)
	if err != nil {
		ip = req.RemoteAddr
	}
	v := &viewer{
		Viewer: Viewer{
			ID:          uuid.NewString(),
			Host:        r.host(req.Context(), ip),
			IP:          ip,
			ConnectedAt: float64(time.Now().UnixMicro()) / 1e6,
		},
		send: make(chan Message, 1),
	}

	ctx := conn.CloseRead(req.Context())
	done := make(chan struct{})
	go func() {
		defer close(done)
		for msg := range v.send {
			writeCtx, cancel := context.WithTimeout(ctx, writeTimeout)
			err := wsjson.Write(writeCtx, conn, msg)
			cancel()
			if err != nil {
				// A socket that died is removed when its read side sees
				// the close.
				r.log.Debug().Err(err).Str("ip", ip).Msg("presence send failed")
				conn.CloseNow()
				return
			}
		}
	}()

	n := r.join(v)
	r.log.Info().Str("ip", ip).Int("viewers", n).Msg("viewing the GCS")
	<-ctx.Done()
	n = r.leave(v)
	r.log.Info().Str("ip", ip).Int("viewers", n).Msg("left the GCS")
	close(v.send)
	<-done
	conn.Close(websocket.StatusNormalClosure, "")
}

// host is the cached or freshly looked-up name of ip, or ip itself when it
// has none or the lookup takes too long.
func (r *Registry) host(ctx context.Context, ip string) string {
	r.mu.Lock()
	host, ok := r.hosts[ip]
	r.mu.Unlock()
	if ok {
		return host
	}
	ctx, cancel := context.WithTimeout(ctx, r.ResolveTimeout)
	defer cancel()
	names, err := r.LookupAddr(ctx, ip)
	if err != nil || len(names) == 0 {
		return ip
	}
	host = strings.TrimSuffix(names[0], ".")
	r.mu.Lock()
	r.hosts[ip] = host
	r.mu.Unlock()
	return host
}

// join lists v and returns how many viewers there now are.
func (r *Registry) join(v *viewer) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.viewers = append(r.viewers, v)
	r.broadcast()
	return len(r.viewers)
}

// leave unlists v and returns how many viewers there now are.
func (r *Registry) leave(v *viewer) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i, other := range r.viewers {
		if other == v {
			r.viewers = append(r.viewers[:i], r.viewers[i+1:]...)
			r.broadcast()
			break
		}
	}
	return len(r.viewers)
}

// broadcast queues the current list for every viewer, replacing any list
// still waiting to be sent. Called with mu held, so every viewer's queue
// sees the lists in the same order.
func (r *Registry) broadcast() {
	list := make([]Viewer, len(r.viewers))
	for i, v := range r.viewers {
		list[i] = v.Viewer
	}
	for _, v := range r.viewers {
		select {
		case <-v.send:
		default:
		}
		v.send <- Message{Viewers: list, You: v.ID}
	}
}
