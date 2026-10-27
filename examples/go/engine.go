package main

import (
	"encoding/json"
	"fmt"
)

type EngineArgs struct {
	Confidence float32 `json:"confidence"`
}

func RunEngine(iceoryx string, args string) {
	fmt.Println("Ran engine")
	if args == "" {
		fmt.Println("No args provided")
		return
	}
	if iceoryx == "" {
		fmt.Println("No iceoryx provided")
		return
	}
	fmt.Printf("Serving at endpoint %s\n", iceoryx)
	var engineArgs EngineArgs
	err := json.Unmarshal([]byte(args), &engineArgs)
	if err != nil {
		fmt.Println("Args could not be parsed or don't contain confidence")
		return
	}
	fmt.Printf("Using confidence %.2f\n", engineArgs.Confidence)
}
