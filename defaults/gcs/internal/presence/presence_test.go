package presence

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"github.com/rs/zerolog"
)

// serve serves r and returns its websocket URL.
func serve(t *testing.T, r *Registry) string {
	t.Helper()
	server := httptest.NewServer(r)
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

// newRegistry is a registry whose lookups resolve 127.0.0.1 to name, or
// fail when name is empty.
func newRegistry(name string) *Registry {
	r := New(zerolog.Nop())
	r.LookupAddr = func(context.Context, string) ([]string, error) {
		if name == "" {
			return nil, errors.New("no name")
		}
		return []string{name + "."}, nil
	}
	return r
}

func dial(t *testing.T, url string) *websocket.Conn {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { conn.CloseNow() })
	return conn
}

func read(t *testing.T, conn *websocket.Conn) Message {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var msg Message
	if err := wsjson.Read(ctx, conn, &msg); err != nil {
		t.Fatalf("read: %v", err)
	}
	return msg
}

func ids(msg Message) []string {
	var out []string
	for _, v := range msg.Viewers {
		out = append(out, v.ID)
	}
	return out
}

func TestJoinListsTheViewerAsYou(t *testing.T) {
	url := serve(t, newRegistry("lab-pc"))
	msg := read(t, dial(t, url))
	if len(msg.Viewers) != 1 {
		t.Fatalf("viewers = %+v, want one", msg.Viewers)
	}
	v := msg.Viewers[0]
	if v.ID != msg.You || v.Host != "lab-pc" || v.IP != "127.0.0.1" || v.ConnectedAt == 0 {
		t.Errorf("viewer = %+v, you = %q", v, msg.You)
	}
}

func TestJoinAndLeaveAreSentToEveryViewer(t *testing.T) {
	url := serve(t, newRegistry("lab-pc"))
	a := dial(t, url)
	first := read(t, a)
	b := dial(t, url)

	fromA, fromB := read(t, a), read(t, b)
	if len(fromA.Viewers) != 2 || strings.Join(ids(fromA), ",") != strings.Join(ids(fromB), ",") {
		t.Fatalf("lists differ or are short: %+v vs %+v", fromA.Viewers, fromB.Viewers)
	}
	if fromA.You != first.You || fromB.You == fromA.You || fromA.Viewers[0].ID != fromA.You {
		t.Errorf("you: a=%q b=%q, list in join order %v", fromA.You, fromB.You, ids(fromA))
	}

	b.Close(websocket.StatusNormalClosure, "")
	left := read(t, a)
	if len(left.Viewers) != 1 || left.Viewers[0].ID != fromA.You {
		t.Errorf("after b left: %+v", left.Viewers)
	}
}

func TestUnresolvableHostIsListedByIP(t *testing.T) {
	url := serve(t, newRegistry(""))
	if host := read(t, dial(t, url)).Viewers[0].Host; host != "127.0.0.1" {
		t.Errorf("host = %q, want the IP", host)
	}
}

func TestSlowLookupGivesUpAndIsRetriedNextJoin(t *testing.T) {
	r := New(zerolog.Nop())
	r.ResolveTimeout = 50 * time.Millisecond
	var slow atomic.Bool
	slow.Store(true)
	r.LookupAddr = func(ctx context.Context, _ string) ([]string, error) {
		if slow.Load() {
			<-ctx.Done()
			return nil, ctx.Err()
		}
		return []string{"lab-pc."}, nil
	}
	url := serve(t, r)
	a := dial(t, url)
	if host := read(t, a).Viewers[0].Host; host != "127.0.0.1" {
		t.Errorf("host = %q, want the IP after a timed-out lookup", host)
	}
	slow.Store(false)
	b := dial(t, url)
	read(t, a)
	if host := read(t, b).Viewers[1].Host; host != "lab-pc" {
		t.Errorf("host = %q, want the name on the next join", host)
	}
}

func TestPlainHTTPIsRefused(t *testing.T) {
	server := httptest.NewServer(newRegistry("lab-pc"))
	defer server.Close()
	resp, err := http.Get(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode < 400 {
		t.Errorf("status = %d, want an error for a non-websocket request", resp.StatusCode)
	}
}
