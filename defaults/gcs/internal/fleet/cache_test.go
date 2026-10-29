package fleet

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"google.golang.org/grpc"
)

// countingDaemon answers ListServices after release is closed (if set),
// counting the calls and checking each has a deadline.
type countingDaemon struct {
	daemonpb.DaemonServiceClient
	calls   atomic.Int32
	release chan struct{}
	err     error
	noDL    atomic.Bool
}

func (d *countingDaemon) ListServices(ctx context.Context, _ *daemonpb.ListServicesRequest, _ ...grpc.CallOption) (*daemonpb.ListServicesResponse, error) {
	n := d.calls.Add(1)
	if _, ok := ctx.Deadline(); !ok {
		d.noDL.Store(true)
	}
	if d.release != nil {
		<-d.release
	}
	if d.err != nil {
		return nil, d.err
	}
	return daemonpb.ListServicesResponse_builder{Services: []*daemonpb.Service{
		daemonpb.Service_builder{Name: string(rune('a' + n - 1))}.Build(),
	}}.Build(), nil
}

// cacheClock is a settable clock.
type cacheClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *cacheClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *cacheClock) advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

func newTestCache(d *countingDaemon) (*CachedDaemon, *cacheClock) {
	clock := &cacheClock{t: time.Unix(1000, 0)}
	c := NewCachedDaemon(d, time.Second, time.Minute)
	c.now = clock.now
	return c, clock
}

func listName(t *testing.T, c *CachedDaemon) string {
	t.Helper()
	resp, err := c.ListServices(context.Background(), &daemonpb.ListServicesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return resp.GetServices()[0].GetName()
}

func TestCachedDaemonSharesWithinTTL(t *testing.T) {
	d := &countingDaemon{}
	c, clock := newTestCache(d)
	if got := listName(t, c); got != "a" {
		t.Fatalf("first listing = %q, want a", got)
	}
	clock.advance(999 * time.Millisecond)
	if got := listName(t, c); got != "a" {
		t.Fatalf("listing within the TTL = %q, want the cached a", got)
	}
	clock.advance(time.Millisecond)
	if got := listName(t, c); got != "b" {
		t.Fatalf("listing after the TTL = %q, want a fresh b", got)
	}
	if n := d.calls.Load(); n != 2 {
		t.Errorf("daemon listed %d times, want 2", n)
	}
	if d.noDL.Load() {
		t.Error("a call to the daemon had no deadline")
	}
}

func TestCachedDaemonCoalescesConcurrentCallers(t *testing.T) {
	d := &countingDaemon{release: make(chan struct{})}
	c, _ := newTestCache(d)
	var wg sync.WaitGroup
	names := make([]string, 20)
	for i := range names {
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := c.ListServices(context.Background(), &daemonpb.ListServicesRequest{})
			if err == nil {
				names[i] = resp.GetServices()[0].GetName()
			}
		}()
	}
	// Let the callers pile up on the call in flight, then answer it
	for d.calls.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	time.Sleep(20 * time.Millisecond)
	close(d.release)
	wg.Wait()
	if n := d.calls.Load(); n != 1 {
		t.Errorf("daemon listed %d times for 20 concurrent callers, want 1", n)
	}
	for i, n := range names {
		if n != "a" {
			t.Errorf("caller %d got %q, want a", i, n)
		}
	}
}

func TestCachedDaemonSharesFailures(t *testing.T) {
	d := &countingDaemon{err: errors.New("deadline exceeded")}
	c, clock := newTestCache(d)
	for range 3 {
		if _, err := c.ListServices(context.Background(), &daemonpb.ListServicesRequest{}); err == nil {
			t.Fatal("want the daemon's error")
		}
	}
	if n := d.calls.Load(); n != 1 {
		t.Errorf("daemon listed %d times within the TTL after a failure, want 1", n)
	}
	d.err = nil
	clock.advance(time.Second)
	if got := listName(t, c); got != "b" {
		t.Errorf("listing after the TTL = %q, want b", got)
	}
}

func TestCachedDaemonCallerGivesUpAlone(t *testing.T) {
	d := &countingDaemon{release: make(chan struct{})}
	c, _ := newTestCache(d)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := c.ListServices(ctx, &daemonpb.ListServicesRequest{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	// The call it started still answers the next caller
	close(d.release)
	if got := listName(t, c); got != "a" {
		t.Errorf("listing = %q, want a from the call already made", got)
	}
	if n := d.calls.Load(); n != 1 {
		t.Errorf("daemon listed %d times, want 1", n)
	}
}
