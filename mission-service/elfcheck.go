package main

import (
	"bytes"
	"debug/elf"
	"encoding/binary"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// elfPrefixLen is how many leading bytes checkELF needs: through e_machine.
const elfPrefixLen = 20

// goarchMachines maps a GOARCH to the ELF machine its binaries are built for.
var goarchMachines = map[string]elf.Machine{
	"amd64": elf.EM_X86_64,
	"arm64": elf.EM_AARCH64,
}

// checkELF rejects prefix unless it starts an ELF file built for goarch, so a
// binary for the wrong architecture is refused early instead of failing at
// StartMission with "exec format error". Hosts with no entry in
// goarchMachines skip the machine check.
func checkELF(prefix []byte, goarch string) error {
	if len(prefix) < elfPrefixLen || !bytes.Equal(prefix[:4], []byte(elf.ELFMAG)) {
		return status.Error(codes.InvalidArgument, "mission is not an ELF executable")
	}
	var order binary.ByteOrder
	switch elf.Data(prefix[elf.EI_DATA]) {
	case elf.ELFDATA2LSB:
		order = binary.LittleEndian
	case elf.ELFDATA2MSB:
		order = binary.BigEndian
	default:
		return status.Error(codes.InvalidArgument, "mission is not an ELF executable (unknown byte order)")
	}
	want, ok := goarchMachines[goarch]
	if !ok {
		return nil
	}
	if got := elf.Machine(order.Uint16(prefix[18:20])); got != want {
		return status.Errorf(codes.InvalidArgument, "mission binary is built for %s, this vehicle needs %s", got, want)
	}
	return nil
}
