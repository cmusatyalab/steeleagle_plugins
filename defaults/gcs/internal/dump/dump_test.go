package dump

import (
	"bytes"
	"encoding/csv"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func frame(id uint64, keyframe bool, data string) *framepb.EncodedFrame {
	return framepb.EncodedFrame_builder{
		EncodedData: []byte(data),
		Id:          id,
		Codec:       framepb.Codec_CODEC_H264,
		Keyframe:    keyframe,
		Timestamp:   timestamppb.New(time.Unix(100, 0)),
	}.Build()
}

// readDump returns the one .h264 file and the rows of the one .csv file in
// dir whose names start with prefix.
func readDump(t *testing.T, dir, prefix string) ([]byte, [][]string) {
	t.Helper()
	h264, _ := filepath.Glob(filepath.Join(dir, prefix+"*.h264"))
	csvs, _ := filepath.Glob(filepath.Join(dir, prefix+"*.csv"))
	if len(h264) != 1 || len(csvs) != 1 {
		t.Fatalf("dump files for %q: %v %v, want one .h264 and one .csv", prefix, h264, csvs)
	}
	data, err := os.ReadFile(h264[0])
	if err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(csvs[0])
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	rows, err := csv.NewReader(f).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	return data, rows
}

func TestRecorderWritesFramesAndGaps(t *testing.T) {
	dir := t.TempDir()
	var logs bytes.Buffer
	r, err := New(dir, zerolog.New(&logs))
	if err != nil {
		t.Fatal(err)
	}
	at := time.Unix(100, int64(50*time.Millisecond))
	r.Write("10.0.0.1.drone.front", 7, frame(0, true, "K0"), at)
	r.Write("10.0.0.1.drone.front", 8, frame(1, false, "P1"), at)
	// Frames 2 and 3 never arrive
	r.Write("10.0.0.1.drone.front", 12, frame(4, false, "P4"), at)
	// The encoder restarted its stream, numbering from 0 again
	r.Write("10.0.0.1.drone.front", 13, frame(0, true, "K0'"), at)
	r.Close()

	data, rows := readDump(t, dir, "10.0.0.1.drone.front")
	if string(data) != "K0P1P4K0'" {
		t.Errorf("h264 = %q, want every frame's data in order", data)
	}
	want := [][]string{
		{"recv_unix_ns", "capture_unix_ns", "latency_ms", "uriel_seq", "frame_id", "keyframe", "bytes", "missed_frames"},
		{"100050000000", "100000000000", "50.0", "7", "0", "true", "2", "0"},
		{"100050000000", "100000000000", "50.0", "8", "1", "false", "2", "0"},
		{"100050000000", "100000000000", "50.0", "12", "4", "false", "2", "2"},
		{"100050000000", "100000000000", "50.0", "13", "0", "true", "3", "0"},
	}
	if len(rows) != len(want) {
		t.Fatalf("csv rows = %v, want %v", rows, want)
	}
	for i := range want {
		if strings.Join(rows[i], ",") != strings.Join(want[i], ",") {
			t.Errorf("csv row %d = %v, want %v", i, rows[i], want[i])
		}
	}
	if !strings.Contains(logs.String(), `"missed":2`) {
		t.Errorf("logs = %s, want a warning that 2 frames were missed", logs.String())
	}
	if !strings.Contains(logs.String(), "restarted") {
		t.Errorf("logs = %s, want a note that the stream restarted", logs.String())
	}
}

func TestRecorderKeepsCamerasApart(t *testing.T) {
	dir := t.TempDir()
	r, err := New(dir, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	r.Write("drone.front", 1, frame(0, true, "A"), time.Now())
	r.Write("drone.gimbal/rear", 1, frame(0, true, "B"), time.Now())
	r.Close()

	if data, _ := readDump(t, dir, "drone.front"); string(data) != "A" {
		t.Errorf("front h264 = %q, want A", data)
	}
	// A slash in the producer name can't be in a file name
	if data, _ := readDump(t, dir, "drone.gimbal_rear"); string(data) != "B" {
		t.Errorf("rear h264 = %q, want B", data)
	}
}

func TestRecorderSkipsFramesThatArentH264(t *testing.T) {
	dir := t.TempDir()
	r, err := New(dir, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	f := frame(0, true, "J")
	f.SetCodec(framepb.Codec_CODEC_JPEG)
	r.Write("drone.front", 1, f, time.Now())
	r.Close()

	if files, _ := os.ReadDir(dir); len(files) != 0 {
		t.Errorf("dump dir has %d files, want none for a JPEG frame", len(files))
	}
}
