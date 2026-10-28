package main

import (
	"fmt"
	"os"
)

func main() {
	plugin := os.Getenv("STEELEAGLE_PLUGIN")
	args := os.Getenv("STEELEAGLE_PLUGIN_ARGS")
	iceoryx := os.Getenv("STEELEAGLE_ENGINE_ENDPOINT")

	if plugin == "driver.plugin.toml" {
		RunDriver(args)
	} else if plugin == "engine.plugin.toml" {
		RunEngine(iceoryx, args)
	} else {
		fmt.Println("Unrecognized plugin!")
	}
}
