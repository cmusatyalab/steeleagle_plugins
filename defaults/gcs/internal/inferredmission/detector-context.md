# Detector evidence in Inferred Mission V7

Plan → Inferred Mission uses sampled images, available vehicle telemetry, and
any matched object detections to recommend a continuation and explain the
evidence. V7 asks what the pilot appears to be doing. The model can infer
tracking from images alone because route availability is checked after the
model responds. GCS validates the recommendation against the selected vehicle
and publishes one compiled mission. The GCS in this checkout pins SDK v0.1.3
in [go.mod](../../go.mod).
See the [V7 prompt](prompts/v7.md) and [standard GCS setup](../../README.md#ai-chat-and-inferred-mission).

## Evidence and bindings

- Start a live run on the selected vehicle and camera. The GCS finds a single
  declared object detector on its local daemon that is reachable from that
  producer in the vehicle's active flow. A shared `DetectionResult` message type
  alone does not establish that an engine is an object detector. When no single
  detector can be selected, the run can still use images and telemetry. The
  subscription works independently of the Control viewer. [Detector selection](../../cmd/gcs/ai.go)
- Results join sampled frames by Uriel producer, session, and sequence. The
  section records up to eight boxes for each matched sample, best first,
  numbered, with their native labels, confidences and positions. Frames are
  numbered alike in the image captions, so the model can name the box it
  judges the pilot to be following. Missing, late, and empty results
  contribute no class observation.
  Frame metadata and nearby vehicle telemetry can add relative altitude, motion,
  gimbal pose, or battery when reported. MP4 uploads supply sampled images and
  their timestamps. [Live capture](live.go) · [Evidence cache](evidence.go)
- V7 uses the image sequence and motion to assess whether the pilot appears to
  be following an object. The matched labels and confidence scores can support
  class identification. [V7 prompt](prompts/v7.md)
- `Track` requires a verified camera-to-`DetectionEngine` route in the planned
  flow, which can differ from the active flow used for observations. A matching
  native class from the same producer and detector can resolve competing routes.
  GCS currently exposes these routes without an authoritative detector class
  list. A single compatible route can still compile with an image-inferred
  class. At runtime the vehicle needs a reachable detector that emits the
  selected class. For a link-loss test, verify that the route remains reachable
  from the vehicle after disconnection.
  [Planning bindings](../mission/bindings.go) · [Track route selection](vision_v6.go)

When the evidence leaves the activity unclear or GCS cannot bind or compile
the proposed continuation, GCS tries **Wait 30 seconds, then return home**.
It uses the selected vehicle's driver
and can compile independently of a broken detector route or map polygon. The
section keeps the inferred activity and the evidence when GCS uses the fallback.
**Apply to Mission Builder** opens the compiled mission for review before
deployment. Existing V5 and V6 run records remain available and any resumed
run uses its recorded prompt.

## Verifying the pipeline in Aviary

1. Use a simulated vehicle with a camera feeding a declared local detection
   engine in its active flow. Select that camera in Plan and use a corresponding
   verified route in the planning flow. Start a live observation with a scene
   containing a class the detector can emit.
2. In the completed section's History, inspect **Matched frame evidence**.
   Check the producer, detector, `sampled_frames`, and
   `frames_with_detector_labels`. Confirm that the sampled frames have the
   expected native labels. Check the section's inference and its evidence,
   then confirm the compile status and saved V4 graph. For `Track`, verify
   the class, camera, and engine in the graph. [Section details](../../web/src/inferred-mission/InferredMissionPage.jsx)
3. Exercise a no-label or wrong-source control and check that labels from other
   frames or vehicles never attach to the samples. With ambiguous intent, check
   that GCS compiles the wait-and-return fallback. The
   [frame matching tests](evidence_test.go),
   [detector-to-planner regression](detector_pipeline_test.go), and
   [mock SDK compilation tests](../../cmd/gcsmock/nl2dsl_test.go) exercise
   those checks.

For uploaded video, test a clear following clip with a verified planning route
and again with that route omitted. Review the inferred activity and evidence in
both runs. Check for a compiled `Track` mission with the route and a compiled
fallback when the model recommends `Track` and GCS cannot bind it.

For a model-quality comparison, hold the sampled images, telemetry, model,
settings, and scene constant and include or omit only the matched detections.
Review the chosen action and target against the known simulated scenario and
record compiler success separately. A scripted provider or fixture label is
sufficient to exercise delivery, binding, and compilation. Claims about model
quality require real model responses and representative scenes.
