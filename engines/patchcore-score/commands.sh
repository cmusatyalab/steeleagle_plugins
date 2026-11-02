echo "Running patchcore-score engine command string!"
STEELEAGLE_PLUGIN=score.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"threshold_meta": "models/gravel_meta.pt", "faiss_index": "models/gravel.faiss"}' STEELEAGLE_ENGINE_ENDPOINT=patchcore_score pixi run start
