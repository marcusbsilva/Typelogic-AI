# Qwen3.5-2B specialist: base, adapter and profiles

The TypeLogic specialist uses **Qwen/Qwen3.5-2B** as its base, with a LoRA adapter and a pointer head trained by this project. Only the text component of the model is used.

| Item | Value |
| --- | --- |
| Repository | https://huggingface.co/Qwen/Qwen3.5-2B (Apache-2.0) |
| Pinned revision | `15852e8c16360a2fea060d615a32b45270f8a8fc` |
| Hidden size | 2048; 24 layers, linear (Gated DeltaNet) and full attention |
| Weights file | `model.safetensors-00001-of-00001.safetensors`, 4,548,221,488 bytes |
| Weights SHA-256 | `aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1` |
| Active profile | `models/decision-2b-distill-s1024` (`models/active.json`) |
| Adapter | LoRA rank 16, alpha 32, pointer head of dimension 256 (88 MB) |
| Scenario limit | 2,048 tokens at inference; training used states of up to 1,024 tokens |

The hashes of all files are in `research/qwen35-2b-assets.json` (base) and in `models/decision-2b-distill-s1024/manifest.json` (base + adapter). The engine checks these hashes on load and refuses altered files.

## Install

The normal path is `TypeLogic.bat models` (or `python tools/fetch_models.py`), which downloads the base at the pinned revision, installs the adapter from the release zip and verifies everything. See the "Models you need to download" section of the [README](../README.md).

Manual installation, without the script:

1. Download the files of the [pinned revision](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc) listed in `research/qwen35-2b-assets.json` into `models/decision-2b-distill-s1024/backbone/`. Do not replace the `asset-provenance.json` that already ships with the repository: its bytes are part of the profile fingerprint.
2. Verify with `python tools/install_models.py --model-dir models/decision-2b-distill-s1024` (no network; it also imports a local copy of the weights file with `--weights PATH`).
3. Extract `typelogic-adapter-decision-2b-distill-s1024.zip` so that `models/decision-2b-distill-s1024/adapter/head.pt` exists.

## Profiles

`python tools/run_app.py profiles` lists the profiles installed in `models/`, and `--use NAME` changes the active one (it writes `models/active.json`). A calibration (`calibration.json`) only applies to the checkpoint whose fingerprint it records.

## How the current profile was trained

- Starting point: the previous adapter (BANKING77 catalog v2), with `tools/train_colab_v2.py --init-from`.
- Data: `research/data/typelogic-mix-v1-s1024` (manifest in the repository): `typelogic-distill-v1` plus replay of the audited BANKING77, states of up to 1,024 tokens.
- Training: Intel Arc B580, float16, 282 updates, best epoch 1, validation accuracy 92.1%. Configuration: `research/training-2b-xpu-distill-s1024.json`; log: `research/runs/b580-xpu-distill-s1024/checkpoint.json`.
- Calibration: temperature 1.40 fitted on the calibration split; BANKING77 test NLL from 0.368 to 0.333 and ECE from 4.1% to 2.2%.
- Results: BANKING77 91.85% (8,260 questions), in-house test 2 79.2%, public JevBench 62.8%.

To train again, see [TRAINING-INTEL-ARC.md](TRAINING-INTEL-ARC.md) and section 6 of the [guide](GUIDE.md). The files that define how the model reads a decision are frozen by `MODEL-PARITY.json` ([analysis](MODEL-PARITY-ANALYSIS.md)).

## Inference

The engine mode (CPU, GPU or Hybrid) is chosen in the web app (Engine & privacy page), with `--mode` or in the launcher.

- CPU: float32 by default (about 9 GB of RAM); `TYPELOGIC_CPU_PRECISION=bfloat16` uses about 4.5 GB and is the default of `TypeLogic.bat start --mode hybrid`.
- Intel Arc: `TypeLogic.bat start --mode gpu`, float16 on the GPU, about 0.14 s per short question.
- The decision head can be read by ONNX Runtime, Rust or PyTorch (`TYPELOGIC_READOUT`); numerical parity is checked on load.
- Warnings about `causal_conv1d` and `flash-linear-attention` indicate correct but slower reference implementations (see [OPTIONAL-COMPONENTS.md](OPTIONAL-COMPONENTS.md)).

`TypeLogic.bat diagnose` checks the dependencies and the selected checkpoint. Without weights or without the adapter, the engine is unavailable with a clear message; there is no fallback download or remote service.
