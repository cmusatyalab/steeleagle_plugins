package main

import (
	"encoding/json"
	"fmt"
)

type DriverArgs struct {
	Message string `json:"message"`
}

func RunDriver(args string) {
	fmt.Println("Ran driver")
	if args == "" {
		fmt.Println("No args provided")
		return
	}
	var driverArgs DriverArgs
	err := json.Unmarshal([]byte(args), &driverArgs)
	if err != nil {
		fmt.Println("Args could not be parsed or don't contain message")
		return
	}
	fmt.Printf("Using message %s\n", driverArgs.Message)
}
