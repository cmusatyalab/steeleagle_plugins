package mission

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// MaxMissionSize is the largest mission binary accepted, in bytes.
const MaxMissionSize = 128 << 20

// elfMachines maps ELF e_machine to GOARCH.
var elfMachines = map[uint16]string{62: "amd64", 183: "arm64"}

// ElfArch returns the GOARCH data was built for, read from its ELF header so
// uploads never depend on filenames or operator labels. It errors if data
// isn't an ELF file or targets an unsupported machine.
func ElfArch(data []byte) (string, error) {
	if len(data) < 20 || string(data[:4]) != "\x7fELF" {
		return "", errors.New("not an ELF executable")
	}
	var order binary.ByteOrder
	switch data[5] {
	case 1:
		order = binary.LittleEndian
	case 2:
		order = binary.BigEndian
	default:
		return "", errors.New("not an ELF executable (unknown byte order)")
	}
	machine := order.Uint16(data[18:20])
	arch, ok := elfMachines[machine]
	if !ok {
		return "", fmt.Errorf("unsupported ELF machine type %d", machine)
	}
	return arch, nil
}
