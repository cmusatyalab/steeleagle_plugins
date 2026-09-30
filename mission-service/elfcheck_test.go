package main

import (
	"debug/elf"
	"encoding/binary"
	"testing"

	"google.golang.org/grpc/codes"
)

// fakeELF returns a minimal 64-byte little-endian ELF64 header for machine.
func fakeELF(machine elf.Machine) []byte {
	b := make([]byte, 64)
	copy(b, elf.ELFMAG)
	b[elf.EI_CLASS] = byte(elf.ELFCLASS64)
	b[elf.EI_DATA] = byte(elf.ELFDATA2LSB)
	b[elf.EI_VERSION] = byte(elf.EV_CURRENT)
	binary.LittleEndian.PutUint16(b[16:18], uint16(elf.ET_EXEC))
	binary.LittleEndian.PutUint16(b[18:20], uint16(machine))
	return b
}

func TestCheckELFAcceptsMatchingMachine(t *testing.T) {
	if err := checkELF(fakeELF(elf.EM_X86_64), "amd64"); err != nil {
		t.Fatalf("checkELF(amd64 binary, amd64) = %v, want nil", err)
	}
	if err := checkELF(fakeELF(elf.EM_AARCH64), "arm64"); err != nil {
		t.Fatalf("checkELF(arm64 binary, arm64) = %v, want nil", err)
	}
}

func TestCheckELFRejectsWrongMachine(t *testing.T) {
	wantCode(t, checkELF(fakeELF(elf.EM_X86_64), "arm64"), codes.InvalidArgument)
}

func TestCheckELFRejectsNonELF(t *testing.T) {
	wantCode(t, checkELF([]byte("#!/bin/sh\necho definitely not a mission\n"), "amd64"), codes.InvalidArgument)
}

func TestCheckELFRejectsTooShort(t *testing.T) {
	wantCode(t, checkELF([]byte{0x7f, 'E', 'L', 'F'}, "amd64"), codes.InvalidArgument)
}

func TestCheckELFSkipsUnknownHostArch(t *testing.T) {
	if err := checkELF(fakeELF(elf.EM_X86_64), "riscv64"); err != nil {
		t.Fatalf("checkELF on an unmapped host arch = %v, want nil (skip)", err)
	}
}
