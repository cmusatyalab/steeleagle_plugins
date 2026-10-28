package main

import (
	"encoding/json"
	"fmt"
)

// Default args, matching engine.plugin.toml. The daemon passes only the args
// set in its config, so any arg left unset is defaulted here.
const defaultConfidence float32 = 0.75

type EngineArgs struct {
	Confidence float32 `json:"confidence"`
}

func RunEngine(iceoryx string, args string) {
	fmt.Println("Ran engine")
	if iceoryx == "" {
		fmt.Println("No iceoryx provided")
		return
	}
	fmt.Printf("Serving at endpoint %s\n", iceoryx)
	engineArgs := EngineArgs{Confidence: defaultConfidence}
	if args != "" {
		if err := json.Unmarshal([]byte(args), &engineArgs); err != nil {
			fmt.Println("Args could not be parsed")
			return
		}
	}
	fmt.Printf("Using confidence %.2f\n", engineArgs.Confidence)
}
