//! How the SteelEagle daemon starts a plugin: it names the plugin to run in `STEELEAGLE_PLUGIN`
//! (the file name of its `.plugin.toml`), its args in `STEELEAGLE_PLUGIN_ARGS` (a JSON object),
//! the name to serve the engine as, already qualified with the local daemon's IP, in
//! `STEELEAGLE_ENGINE_ENDPOINT`, and the remote daemon's IP, if there is one, in
//! `STEELEAGLE_REMOTE_QUALIFIER`.

use std::env;

use anyhow::{Context, Result, bail};
use serde::Deserialize;

use crate::{decode, encode};

const PLUGIN_VAR: &str = "STEELEAGLE_PLUGIN";
const ARGS_VAR: &str = "STEELEAGLE_PLUGIN_ARGS";
const ENDPOINT_VAR: &str = "STEELEAGLE_ENGINE_ENDPOINT";
const REMOTE_QUALIFIER_VAR: &str = "STEELEAGLE_REMOTE_QUALIFIER";

pub const ENCODER_PLUGIN: &str = "encoder.plugin.toml";
pub const DECODER_PLUGIN: &str = "decoder.plugin.toml";
/// The service name of the decoder on the remote daemon, unless the encoder is told otherwise.
pub const DEFAULT_DECODER_NAME: &str = "h264_decoder";

/// What the daemon asked for.
pub enum Plugin {
    Encoder(encode::Config),
    Decoder(decode::Config),
}

/// The args of the encoder plugin (`encoder.plugin.toml`). The daemon sends only the args set in
/// its config, so any may be missing.
#[derive(Default, Deserialize)]
struct EncoderArgs {
    decoder_name: Option<String>,
    encoder: Option<String>,
    input_format: Option<String>,
    fps: Option<u32>,
    max_output_size: Option<usize>,
    timeout_ms: Option<u64>,
    log_frames: Option<bool>,
}

/// The args of the decoder plugin (`decoder.plugin.toml`).
#[derive(Default, Deserialize)]
struct DecoderArgs {
    decoder: Option<String>,
    max_width: Option<usize>,
    max_height: Option<usize>,
    timeout_ms: Option<u64>,
    log_frames: Option<bool>,
}

/// The plugin the daemon's environment describes.
pub fn from_env() -> Result<Plugin> {
    let var = |name: &str| env::var(name).ok();
    let plugin = var(PLUGIN_VAR).with_context(|| format!("{PLUGIN_VAR} is not set"))?;
    let endpoint = var(ENDPOINT_VAR).with_context(|| format!("{ENDPOINT_VAR} is not set"))?;
    parse(
        &plugin,
        &endpoint,
        var(ARGS_VAR).as_deref().unwrap_or_default(),
        var(REMOTE_QUALIFIER_VAR).as_deref().unwrap_or_default(),
    )
}

/// The plugin named `plugin`, served as the engine `endpoint`, with the JSON object `args`. The
/// encoder watches the decoder on the remote daemon `remote_qualifier`, if there is one.
pub fn parse(plugin: &str, endpoint: &str, args: &str, remote_qualifier: &str) -> Result<Plugin> {
    if endpoint.is_empty() {
        bail!("{ENDPOINT_VAR} is empty");
    }
    // An empty value means no args, as does an object without any.
    let args = if args.trim().is_empty() { "{}" } else { args };
    match plugin {
        ENCODER_PLUGIN => {
            let args: EncoderArgs = serde_json::from_str(args).context("invalid encoder args")?;
            let decoder_name = args.decoder_name.as_deref().unwrap_or(DEFAULT_DECODER_NAME);
            Ok(Plugin::Encoder(encode::Config {
                name: endpoint.to_owned(),
                // With no remote daemon, there is no decoder to watch.
                decoder: (!remote_qualifier.is_empty())
                    .then(|| format!("{remote_qualifier}.{decoder_name}")),
                encoder: args
                    .encoder
                    .unwrap_or_else(|| encode::DEFAULT_ENCODER.to_owned()),
                input_format: args
                    .input_format
                    .unwrap_or_else(|| encode::DEFAULT_INPUT_FORMAT.to_owned()),
                fps: args.fps.unwrap_or(encode::DEFAULT_FPS),
                max_output_size: args
                    .max_output_size
                    .unwrap_or(encode::DEFAULT_MAX_OUTPUT_SIZE),
                timeout_ms: args.timeout_ms.unwrap_or(encode::DEFAULT_TIMEOUT_MS),
                log_frames: args.log_frames.unwrap_or_default(),
            }))
        }
        DECODER_PLUGIN => {
            let args: DecoderArgs = serde_json::from_str(args).context("invalid decoder args")?;
            Ok(Plugin::Decoder(decode::Config {
                name: endpoint.to_owned(),
                decoder: args
                    .decoder
                    .unwrap_or_else(|| decode::DEFAULT_DECODER.to_owned()),
                max_width: args.max_width.unwrap_or(decode::DEFAULT_MAX_WIDTH),
                max_height: args.max_height.unwrap_or(decode::DEFAULT_MAX_HEIGHT),
                timeout_ms: args.timeout_ms.unwrap_or(decode::DEFAULT_TIMEOUT_MS),
                log_frames: args.log_frames.unwrap_or_default(),
            }))
        }
        _ => bail!("unrecognized plugin {plugin:?}; expected {ENCODER_PLUGIN} or {DECODER_PLUGIN}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ENDPOINT: &str = "10.0.0.2.h264_encoder";
    const REMOTE: &str = "10.0.0.9";
    const CUSTOM_FPS: u32 = 15;

    fn encoder(args: &str, remote_qualifier: &str) -> encode::Config {
        match parse(ENCODER_PLUGIN, ENDPOINT, args, remote_qualifier).unwrap() {
            Plugin::Encoder(config) => config,
            Plugin::Decoder(_) => panic!("expected the encoder"),
        }
    }

    #[test]
    fn unset_encoder_args_get_their_defaults() {
        let config = encoder("", REMOTE);

        assert_eq!(config.name, ENDPOINT);
        assert_eq!(config.encoder, encode::DEFAULT_ENCODER);
        assert_eq!(config.input_format, encode::DEFAULT_INPUT_FORMAT);
        assert_eq!(config.fps, encode::DEFAULT_FPS);
        assert_eq!(config.max_output_size, encode::DEFAULT_MAX_OUTPUT_SIZE);
        assert_eq!(config.timeout_ms, encode::DEFAULT_TIMEOUT_MS);
        assert!(!config.log_frames);
    }

    #[test]
    fn set_encoder_args_are_used() {
        let config = encoder(
            &format!(r#"{{"fps": {CUSTOM_FPS}, "input_format": "NV12", "log_frames": true}}"#),
            REMOTE,
        );

        assert_eq!(config.fps, CUSTOM_FPS);
        assert_eq!(config.input_format, "NV12");
        assert!(config.log_frames);
    }

    #[test]
    fn encoder_watches_the_decoder_on_the_remote_daemon() {
        assert_eq!(
            encoder("{}", REMOTE).decoder.as_deref(),
            Some("10.0.0.9.h264_decoder")
        );
        assert_eq!(
            encoder(r#"{"decoder_name": "dec"}"#, REMOTE)
                .decoder
                .as_deref(),
            Some("10.0.0.9.dec")
        );
    }

    #[test]
    fn encoder_watches_nothing_without_a_remote_daemon() {
        assert_eq!(encoder("{}", "").decoder, None);
    }

    #[test]
    fn decoder_args_get_their_defaults() {
        let Plugin::Decoder(config) = parse(DECODER_PLUGIN, ENDPOINT, "{}", "").unwrap() else {
            panic!("expected the decoder");
        };

        assert_eq!(config.decoder, decode::DEFAULT_DECODER);
        assert_eq!(config.max_width, decode::DEFAULT_MAX_WIDTH);
        assert_eq!(config.max_height, decode::DEFAULT_MAX_HEIGHT);
    }

    #[test]
    fn bad_input_is_rejected() {
        assert!(parse("other.plugin.toml", ENDPOINT, "{}", "").is_err());
        assert!(parse(ENCODER_PLUGIN, "", "{}", "").is_err());
        assert!(parse(ENCODER_PLUGIN, ENDPOINT, "[1]", "").is_err());
    }
}
