//! The SteelEagle plugin: runs the encoder or the decoder, as the daemon's environment says.

use anyhow::Result;
use h264_rust::{
    decode, encode,
    plugin::{self, Plugin},
    stop_on_signal,
};

fn main() -> Result<()> {
    let stop = stop_on_signal()?;
    match plugin::from_env()? {
        Plugin::Encoder(config) => encode::run(&config, &stop),
        Plugin::Decoder(config) => decode::run(&config, &stop),
    }
}
