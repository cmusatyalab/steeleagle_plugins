package flows

import (
	"strings"
	"testing"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

const (
	encodedFrame = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
	// preprocessConf forwards RawFrames and advertises a producer of EncodedFrames.
	preprocessConf = "[engine]\ntype=\"filter\"\ninputs=[\"RawFrame\", \"Other\"]\nforwards=\"RawFrame\"\n" +
		"[[engine.producers]]\nname=\"frames\"\noutput=\"" + encodedFrame + "\"\n"
	// relayConf forwards whatever it takes and advertises a producer.
	relayConf = "[engine]\ntype=\"generic\"\ninputs=[\"RawFrame\"]\n" +
		"[[engine.producers]]\nname=\"keyframes\"\noutput=\"" + encodedFrame + "\"\n"
)

// producerCatalog is the test catalog plus preprocess on charlie's daemon
// (swap03), relay on the orchestrator (cloudlet033), and relay2 on a daemon
// that is neither.
func producerCatalog() Catalog {
	resp := listResponse()
	services := append(resp.GetServices(),
		svc("100.64.0.2.preprocess", engine, preprocessConf),
		svc("100.64.0.1.relay", engine, relayConf),
		svc("100.64.0.3.relay2", engine, relayConf),
	)
	remotes := append(resp.GetRemotes(), daemonpb.Daemon_builder{Ip: "100.64.0.3", Hostname: "swap04"}.Build())
	return BuildCatalog(daemonpb.ListServicesResponse_builder{Services: services, Remotes: remotes}.Build(),
		"100.64.0.1", "cloudlet033")
}

func TestCatalogEngineForwardsAndProducers(t *testing.T) {
	c := producerCatalog()
	p, ok := c.Engine("100.64.0.2.preprocess")
	if !ok || p.Forwards != "RawFrame" || len(p.Producers) != 1 ||
		p.Producers[0] != (EngineProducer{Name: "frames", Type: encodedFrame}) {
		t.Errorf("preprocess = %+v, %v", p, ok)
	}
	y, _ := c.Engine("100.64.0.2.yolo")
	if y.Forwards != config.ForwardsAll || y.Producers == nil || len(y.Producers) != 0 {
		t.Errorf("yolo forwards %q producers %#v, want * and an empty list", y.Forwards, y.Producers)
	}
	u := untypedEngine("broken")
	if u.Forwards != config.ForwardsAll || u.Producers == nil {
		t.Errorf("untyped = %+v", u)
	}
	// A forwards type that isn't an input is what the engine converts its
	// inputs to, and is passed on as it is.
	conv, err := ParseEngine([]byte("[engine]\ntype=\"x\"\ninputs=[\"A\"]\nforwards=\"B\"\n"))
	if err != nil || conv.Forwards != "B" {
		t.Errorf("converter = %+v, %v, want forwards B", conv, err)
	}
}

func producerTemplate(producer string) Template {
	return Template{
		Vehicle: "100.64.0.2.charlie",
		Producers: []TemplateProducer{
			{Name: "swap03.rgb"},
			{Name: producer, Tokens: intp(3)},
		},
		Engines: []TemplateEngine{{Name: "swap03.preprocess"}, {Name: "cloudlet033.h264_encoder"}},
		Edges: []TemplateEdge{
			{From: "swap03.rgb", To: "swap03.preprocess", Timeout: "1s"},
			{From: producer, To: "cloudlet033.h264_encoder", Timeout: "2s"},
		},
	}
}

func TestResolveEngineProducer(t *testing.T) {
	c := producerCatalog()
	for _, producer := range []string{"swap03.preprocess.frames", "100.64.0.2.preprocess.frames"} {
		r, err := producerTemplate(producer).Resolve(c, "100.64.0.1", charlie(), []string{"rgb"}, "m", nil)
		if err != nil {
			t.Errorf("%s: %v", producer, err)
			continue
		}
		f, err := config.ParseFlowConfigFromBytes(r.Flow)
		if err != nil {
			t.Fatal(err)
		}
		const want = "local.preprocess.frames"
		if f.Producers[0].Name != "vehicle.rgb" || f.Producers[1].Name != want || *f.Producers[1].Tokens != 3 {
			t.Errorf("%s: producers %+v, want vehicle.rgb and %s", producer, f.Producers, want)
		}
		if f.Edges[1].From != want || f.Edges[1].To != "remote.h264_encoder" {
			t.Errorf("%s: edge %+v, want %s to remote.h264_encoder", producer, f.Edges[1], want)
		}
		// Engine producers aren't engines, so the manifest doesn't list them.
		if strings.Contains(string(r.Manifest), "frames") {
			t.Errorf("%s: manifest names the producer:\n%s", producer, r.Manifest)
		}
	}
}

// A vehicle only takes the producers of engines on its own daemon, so a
// producer of an engine on the orchestrator, or anywhere else, is refused.
func TestRemoteEngineProducerRefused(t *testing.T) {
	c := producerCatalog()
	const want = " is of a remote engine; only producers of engines on the vehicle's daemon can be used"
	for _, producer := range []string{"cloudlet033.relay.keyframes", "swap04.relay2.keyframes"} {
		_, err := producerTemplate(producer).Resolve(c, "100.64.0.1", charlie(), []string{"rgb"}, "m", nil)
		wantErr(t, err, "flow producer "+producer+want)
		// Check refuses it too, relative to charlie, which the flow was
		// designed for.
		wantErr(t, producerTemplate(producer).Check(c, ""), "flow producer "+producer+want)
	}
	// The orchestrator's producer is local to a vehicle on the orchestrator's
	// own daemon.
	alpha := fleet.Vehicle{Name: "100.64.0.1.alpha", DaemonIP: "100.64.0.1"}
	tpl := producerTemplate("cloudlet033.relay.keyframes")
	tpl.Vehicle = alpha.Name
	tpl.Engines = []TemplateEngine{{Name: "cloudlet033.h264_encoder"}}
	tpl.Producers = tpl.Producers[1:]
	tpl.Edges = tpl.Edges[1:]
	r, err := tpl.Resolve(c, "100.64.0.1", alpha, []string{"rgb"}, "m", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(r.Flow), `name = "local.relay.keyframes"`) {
		t.Errorf("flow %s, want local.relay.keyframes", r.Flow)
	}
}

func TestEngineProducerErrors(t *testing.T) {
	c := producerCatalog()
	err := producerTemplate("swap03.preprocess.nope").Check(c, "")
	wantErr(t, err, "flow producer swap03.preprocess.nope: engine swap03.preprocess has no producer nope")

	tpl := producerTemplate("swap03.preprocess.frames")
	tpl.Producers = append(tpl.Producers, TemplateProducer{Name: "100.64.0.2.preprocess.frames"})
	tpl.Edges = append(tpl.Edges, TemplateEdge{From: "100.64.0.2.preprocess.frames", To: "swap03.preprocess", Timeout: "1s"})
	wantErr(t, tpl.Check(c, ""), "flow producers swap03.preprocess.frames and 100.64.0.2.preprocess.frames are the same producer")
}

// A {vehicle}.{stream} name whose vehicle part doesn't name an engine stays a
// vehicle stream, even when it has dots.
func TestVehicleStreamNotEngineProducer(t *testing.T) {
	c := producerCatalog()
	if _, ok, err := c.engineProducer("swap03.charlie.rgb"); ok || err != nil {
		t.Errorf("swap03.charlie.rgb: ok %v err %v, want a vehicle stream", ok, err)
	}
	if _, ok, err := c.engineProducer("charlie.rgb"); ok || err != nil {
		t.Errorf("charlie.rgb: ok %v err %v, want a vehicle stream", ok, err)
	}
}
