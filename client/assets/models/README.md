# User-provided wake model

Wake-model weights and training audio, including the custom “Hey Cube” model, are not included. You can inspect, build and test the source without them.

For voice operation, supply a compatible openWakeWord ONNX wake model for which you have the necessary rights:

- Default location: `client/assets/models/hey_cube.onnx` (ignored by Git).
- Override: `CUBE_WAKE_MODEL_PATH=/absolute/path/to/your/model.onnx`.
- Relative overrides resolve against `client/`, independently of the current working directory.

The runtime uses openWakeWord's ONNX interface and its separately provisioned embedding/melspectrogram assets. Obtain supporting assets using the upstream [openWakeWord documentation](https://github.com/dscripka/openWakeWord) in the client environment and review their individual terms. Renaming an arbitrary ONNX file does not make it a compatible wake detector.

`client/app/cube.py` retains the `hey_cube` threshold of 0.50 and a fallback threshold of 0.20 for other prediction names. Review those settings and the backend's leading “Hey Cube” transcript handling when supplying another model/phrase; Cube does not automatically calibrate or retrain a detector. The model determines what phrase is detected.

Missing weights prevent normal voice startup; they are not downloaded silently by Cube. Hardware-free tests mock model loading and test asset-path configuration without reading a private model. Do not commit supplied weights, training data or recordings.
