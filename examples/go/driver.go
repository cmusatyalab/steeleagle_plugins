package main

import (
	"encoding/json"
	"fmt"
)

// Default args, matching driver.plugin.toml. The daemon passes only the args
// set in its config, so any arg left unset is defaulted here.
const defaultMessage = "Hello World!"

type DriverArgs struct {
	Message string `json:"message"`
}

func RunDriver(args string) {
	fmt.Println("Ran driver")
	driverArgs := DriverArgs{Message: defaultMessage}
	if args != "" {
		if err := json.Unmarshal([]byte(args), &driverArgs); err != nil {
			fmt.Println("Args could not be parsed")
			return
		}
	}
	// This should output the message argument
	fmt.Printf("Using message %s\n", driverArgs.Message)
}
