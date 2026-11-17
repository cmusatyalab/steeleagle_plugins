# Detector evidence in Inferred Mission V6

Plan → Inferred Mission combines sampled images, matched object detections, and
available vehicle telemetry into one natural-language recommendation. V6 parses
that recommendation and builds a DSL and graph checked by the selected vehicle's
mission compiler. The GCS in this checkout pins SDK v0.1.3 in [go.mod](../../go.mod).
See the [V6 prompt](prompts/v6.md) and [standard GCS setup](../../README.md#ai-chat-and-inferred-mission).

## Evidence and bindings

- Start a live run on the selected vehicle and camera. The GCS finds a single
  declared object detector on its local daemon that is reachable from that
  producer in the vehicle's active flow. A shared `DetectionResult` message type
  alone does not establish that an engine is an object detector. When no single
  detector can be selected, the run can still use images and telemetry. The
  subscription works independently of the Control viewer. [Detector selection](../../cmd/gcs/ai.go)
- Results join sampled frames by Uriel producer, session, and sequence. The
  section records up to four distinct native labels for each matched sample.
  Each label uses its highest confidence in that frame. Missing, late, and
  empty results contribute no class observation.
  Frame metadata and nearby vehicle telemetry can add relative altitude, motion,
  gimbal pose, or battery when reported. MP4 uploads use sampled imagery without
  the live detector subscription. [Live capture](live.go) · [Evidence cache](evidence.go)
- A label and score describe an object detection. V6 also uses the image
  sequence and motion to assess whether the pilot appears to be following the
  object. The model receives no detector boxes or persistent object IDs.
  [Prompt input and bindings](vision_v6.go)
- `Track` requires a verified camera-to-`DetectionEngine` route in the planned
  flow, which can differ from the active flow used for observations. A matching
  native class from the same producer and detector can resolve competing routes.
  GCS currently exposes these routes without an authoritative detector class
  list. A single compatible route can still compile with an image-inferred
  class. At runtime the vehicle needs a reachable detector that emits the
  selected class. For a link-loss test, verify that the route remains reachable
  from the vehicle after disconnection.
  [Planning bindings](../mission/bindings.go) · [V6 route selection](vision_v6.go)

When the evidence does not support a specific continuation, V6 uses **Wait 30
seconds, then return home**. The same fallback is tried when a proposed
continuation cannot be bound or compiled. It uses the selected vehicle's driver
and can compile independently of a broken detector route or map polygon. Use
**Apply to Mission Builder** in Plan to review a compiled result before
deployment. [V6 generation](vision_v6.go)

## Verifying the pipeline in Aviary

1. Use a simulated vehicle with a camera feeding a declared local detection
   engine in its active flow. Select that camera in Plan and use a corresponding
   verified route in the planning flow. Start a live observation with a scene
   containing a class the detector can emit.
2. In the completed section's History, inspect **Matched frame evidence**.
   Check the producer, detector, `sampled_frames`, and
   `frames_with_detector_labels`. Confirm that the sampled frames have the
   expected native labels. Compare **Vision model reply** with the displayed
   recommendation and confirm the compile status and saved V4 graph. For `Track`,
   verify the class, camera, and engine in the graph. [Section details](../../web/src/inferred-mission/InferredMissionPage.jsx)
3. Exercise a no-label or wrong-source control and check that labels from other
   frames or vehicles never attach to the samples. With ambiguous intent, check
   that V6 generates the wait-and-return fallback. The
   [frame matching tests](evidence_test.go),
   [detector-to-planner regression](detector_pipeline_test.go), and
   [mock SDK compilation tests](../../cmd/gcsmock/nl2dsl_test.go) exercise
   those checks.

For a model-quality comparison, hold the sampled images, telemetry, model,
settings, and scene constant and include or omit only the matched detections.
Review the chosen action and target against the known simulated scenario and
record compiler success separately. A scripted provider or fixture label is
sufficient to exercise delivery, binding, and compilation. Claims about model
quality require real model responses and representative scenes.
