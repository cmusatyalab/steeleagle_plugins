echo "Running patchcore-score engine command string!"
STEELEAGLE_PLUGIN=score.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"bank_ckpt": "models/bank_1000.ckpt", "faiss_index": "models/bank_1000.faiss"}' STEELEAGLE_ENGINE_ENDPOINT=patchcore_score pixi run start
