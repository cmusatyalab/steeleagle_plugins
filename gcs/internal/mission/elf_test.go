package mission

import (
	"encoding/binary"
	"strings"
	"testing"
)

const (
	emX86_64  = 62
	emAArch64 = 183
)

// fakeElf is a little-endian ELF64 header for machine, zero-padded to 64 bytes.
func fakeElf(machine uint16) []byte {
	b := make([]byte, 64)
	copy(b, "\x7fELF")
	b[4], b[5], b[6] = 2, 1, 1               // ELFCLASS64, ELFDATA2LSB, EV_CURRENT
	binary.LittleEndian.PutUint16(b[16:], 2) // ET_EXEC
	binary.LittleEndian.PutUint16(b[18:], machine)
	return b
}

func TestElfArch(t *testing.T) {
	be := fakeElf(0)
	be[5] = 2 // ELFDATA2MSB
	binary.BigEndian.PutUint16(be[18:], emAArch64)
	for name, tc := range map[string]struct {
		data []byte
		want string
	}{
		"amd64":      {fakeElf(emX86_64), "amd64"},
		"arm64":      {fakeElf(emAArch64), "arm64"},
		"big endian": {be, "arm64"},
	} {
		got, err := ElfArch(tc.data)
		if err != nil || got != tc.want {
			t.Errorf("%s: got %q, %v want %q", name, got, err, tc.want)
		}
	}
}

func TestElfArchRejectsNonElf(t *testing.T) {
	for _, data := range [][]byte{nil, []byte("\x7fELF"), []byte("#!/bin/sh\necho not a mission binary at all\n"), make([]byte, 64)} {
		_, err := ElfArch(data)
		if err == nil || !strings.Contains(err.Error(), "not an ELF") {
			t.Errorf("%q: got %v", data, err)
		}
	}
}

func TestElfArchRejectsUnknownByteOrder(t *testing.T) {
	d := fakeElf(emX86_64)
	d[5] = 7
	if _, err := ElfArch(d); err == nil || err.Error() != "not an ELF executable (unknown byte order)" {
		t.Fatalf("got %v", err)
	}
}

func TestElfArchRejectsUnsupportedMachine(t *testing.T) {
	_, err := ElfArch(fakeElf(40))
	if err == nil || err.Error() != "unsupported ELF machine type 40" {
		t.Fatalf("got %v", err)
	}
}
