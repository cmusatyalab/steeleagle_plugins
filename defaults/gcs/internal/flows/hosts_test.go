package flows

import (
	"testing"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
)

func TestSplitTemplateName(t *testing.T) {
	cases := []struct {
		in, host, bare string
		ok             bool
	}{
		{"swap03.yolo", "swap03", "yolo", true},
		{"100.64.0.2.yolo", "100.64.0.2", "yolo", true},
		{"swap03.tail1.ts.net.front", "swap03.tail1.ts.net", "front", true},
		{"yolo", "", "", false},
		{".yolo", "", "", false},
		{"swap03.", "", "", false},
	}
	for _, c := range cases {
		host, bare, ok := SplitTemplateName(c.in)
		if ok != c.ok || (ok && (host != c.host || bare != c.bare)) {
			t.Errorf("SplitTemplateName(%q) = %q, %q, %v", c.in, host, bare, ok)
		}
	}
}

func TestResolveHost(t *testing.T) {
	resp := listResponse()
	resp.SetRemotes(append(resp.GetRemotes(), daemonpb.Daemon_builder{Ip: "100.64.0.4", Hostname: "heisenberg.tail1.ts.net"}.Build()))
	c := BuildCatalog(resp, "100.64.0.1", "cloudlet033")
	for host, want := range map[string]string{
		"100.64.0.2":              "100.64.0.2",
		"swap03":                  "100.64.0.2",
		"SWAP03":                  "100.64.0.2",
		"swap03.tail1.ts.net":     "100.64.0.2",
		"cloudlet033":             "100.64.0.1",
		"heisenberg":              "100.64.0.4",
		"heisenberg.tail1.ts.net": "100.64.0.4",
	} {
		if ip, err := c.ResolveHost(host); err != nil || ip != want {
			t.Errorf("ResolveHost(%q) = %q, %v, want %q", host, ip, err, want)
		}
	}
	for _, host := range []string{"100.64.0.7", "nobody"} {
		_, err := c.ResolveHost(host)
		if want := "flow host " + host + " is not a known daemon"; err == nil || err.Error() != want {
			t.Errorf("ResolveHost(%q) error = %v, want %q", host, err, want)
		}
	}
}

func TestResolveHostLearnedHostname(t *testing.T) {
	// A flow saved with an IP host still resolves once the hostname is known.
	c := BuildCatalog(listResponse(), "100.64.0.1", "cloudlet033")
	for _, host := range []string{"100.64.0.2", "swap03"} {
		if ip, err := c.ResolveHost(host); err != nil || ip != "100.64.0.2" {
			t.Errorf("ResolveHost(%q) = %q, %v", host, ip, err)
		}
	}
}

func mockCatalog() Catalog {
	return BuildCatalog(daemonpb.ListServicesResponse_builder{Remotes: []*daemonpb.Daemon{
		daemonpb.Daemon_builder{Ip: "100.64.0.2", Hostname: "mock-100.64.0.2"}.Build(),
		daemonpb.Daemon_builder{Ip: "100.64.0.3", Hostname: "mock-100.64.0.3"}.Build(),
	}}.Build(), "100.64.0.1", "cloudlet033")
}

func TestResolveHostExactBeatsShortName(t *testing.T) {
	c := mockCatalog()
	for _, ip := range []string{"100.64.0.2", "100.64.0.3"} {
		if got, err := c.ResolveHost("mock-" + ip); err != nil || got != ip {
			t.Errorf("ResolveHost(mock-%s) = %q, %v", ip, got, err)
		}
	}
	_, err := c.ResolveHost("mock-100")
	if want := "flow host mock-100 is ambiguous: it matches 100.64.0.2 and 100.64.0.3"; err == nil || err.Error() != want {
		t.Errorf("error = %v, want %q", err, want)
	}
	for _, d := range c.Daemons {
		if got := c.HostLabel(d); got != d.Hostname {
			t.Errorf("HostLabel(%s) = %q, want its hostname", d.IP, got)
		}
	}
}

func TestHostLabel(t *testing.T) {
	c := BuildCatalog(listResponse(), "100.64.0.1", "cloudlet033")
	if d, _ := c.Daemon("100.64.0.2"); c.HostLabel(d) != "swap03" {
		t.Errorf("label = %q, want swap03", c.HostLabel(d))
	}
	c = BuildCatalog(listResponse(), "100.64.0.1", "")
	if d, _ := c.Daemon("100.64.0.1"); c.HostLabel(d) != "100.64.0.1" {
		t.Errorf("label = %q, want the IP", c.HostLabel(d))
	}
	dup := BuildCatalog(daemonpb.ListServicesResponse_builder{Remotes: []*daemonpb.Daemon{
		daemonpb.Daemon_builder{Ip: "100.64.0.2", Hostname: "dup"}.Build(),
		daemonpb.Daemon_builder{Ip: "100.64.0.3", Hostname: "dup"}.Build(),
	}}.Build(), "100.64.0.1", "")
	for _, d := range dup.Daemons {
		if got := dup.HostLabel(d); got != d.IP {
			t.Errorf("HostLabel(%s) = %q, want the IP", d.IP, got)
		}
	}
}

func TestHostLabelClashWithUnavailable(t *testing.T) {
	c := BuildCatalog(daemonpb.ListServicesResponse_builder{Remotes: []*daemonpb.Daemon{
		daemonpb.Daemon_builder{Ip: "100.64.0.2", Hostname: "eagled"}.Build(),
		daemonpb.Daemon_builder{Ip: "100.64.0.3", Hostname: "Eagled.tail1.ts.net", Error: "unreachable"}.Build(),
		daemonpb.Daemon_builder{Ip: "100.64.0.4", Hostname: "other"}.Build(),
	}}.Build(), "100.64.0.1", "")
	for ip, want := range map[string]string{"100.64.0.2": "100.64.0.2", "100.64.0.4": "other"} {
		d, _ := c.Daemon(ip)
		if got := c.HostLabel(d); got != want {
			t.Errorf("HostLabel(%s) = %q, want %q", ip, got, want)
		}
	}
}
