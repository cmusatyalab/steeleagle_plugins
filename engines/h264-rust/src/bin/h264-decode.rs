//! Decodes H.264 frames to raw RGB frames, as a Uriel engine, started from the command line. The
//! SteelEagle daemon starts `h264-plugin` instead.

use anyhow::Result;
use clap::Parser;
use h264_rust::{decode, stop_on_signal};

#[derive(Parser)]
#[command(
    version,
    about = "Decodes H.264 frames to raw RGB frames, as a Uriel engine"
)]
struct Cli {
    #[command(flatten)]
    config: decode::Config,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let stop = stop_on_signal()?;
    decode::run(&cli.config, &stop)
}
