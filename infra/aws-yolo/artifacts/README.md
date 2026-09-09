# Release bundle staging area

Do not put an unevaluated checkpoint here. The image build requires all three:

- `model.onnx`
- `model-manifest.json`
- `runtime-parity.json`

Create them with `ml/yolo/release.py` after the held-out evaluation passes,
then copy the complete release bundle into this directory. The manifest is checked
again at Lambda cold start: it must identify class `0` as `pothole`, declare a raw
`nms=false` ONNX export, match the configured model version, and carry a sealed
held-out parity receipt for the exact deployed detector code.

Model binaries and generated manifests are ignored by Git.
