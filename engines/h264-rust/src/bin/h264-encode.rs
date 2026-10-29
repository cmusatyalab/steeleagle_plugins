//! Encodes raw RGB frames with H.264, as a Uriel engine, started from the command line. The
//! SteelEagle daemon starts `h264-plugin` instead.

use anyhow::Result;
use clap::Parser;
use h264_rust::{encode, stop_on_signal};

#[derive(Parser)]
#[command(
    version,
    about = "Encodes raw RGB frames with H.264, as a Uriel engine"
)]
struct Cli {
    #[command(flatten)]
    config: encode::Config,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let stop = stop_on_signal()?;
    encode::run(&cli.config, &stop)
}
