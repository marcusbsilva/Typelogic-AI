# Training the specialist on Intel Arc (XPU)

The specialist (Qwen3.5-2B + LoRA + pointer head) can be trained on a 12 GB Intel Arc B580. The active profile `decision-2b-distill-s1024` was trained this way. This path is separate from inference: the application keeps loading the installed profile until you change the active profile.

## Environment

1. Install a current Intel Arc driver and run `TypeLogic.bat setup --gpu`. It creates `%USERPROFILE%\TypeLogic-XPU` with `torch==2.14.1+xpu`, `transformers==5.19.0`, `peft==0.21.2` and the application packages. An existing environment does not have PyTorch reinstalled.
2. Download the models (`TypeLogic.bat models`) and the replay data (`typelogic-training-data-v1.zip` from the release, extracted into the project folder).
3. Check without loading the base:

```powershell
$py = "$env:USERPROFILE\TypeLogic-XPU\Scripts\python.exe"
& $py -c "import torch; print(torch.__version__, torch.xpu.is_available())"
& $py -m pytest tests\test_training_backend.py -q
```

## Measured limits

- fp16 precision (the GPU advertises bf16, but the verified path is fp16).
- States of up to **1,024 tokens** in training: with 1,536 tokens the VRAM overflowed into shared memory. The admission test uses the longest record and requires a peak below 11 GiB.
- About 15 s per example with the reference Gated DeltaNet (without `flash-linear-attention`); training the current profile took 4 to 5 hours.
- Close Ollama (or unload Plumb-4B) before training: the two do not fit together in the 12 GB during training.

## Building the data

```powershell
& $py tools\build_training_mix.py `
  --distill research\data\typelogic-distill-v1 `
  --replay-train research\data\jev-banking77-catalog-v2-audited\train.jsonl `
  --replay-splits research\data\intent-choice-en-v2 `
  --tokenizer models\decision-2b-distill-s1024\backbone --max-state 1024 `
  --feedback research\data\feedback-2026-11.jsonl `
  --out research\data\typelogic-mix-v2
```

`--feedback` is optional and can be repeated (exports from the application, see section 6 of the [guide](GUIDE.md)). The manifest records counts and hashes for each split.

## Mandatory smoke test

In a new folder: pre-encodes the splits, includes the longest training row, runs two updates and checks checkpoint restoration. It does not change the installed profile.

```powershell
& $py -u tools\train_colab_v2.py `
  --device xpu `
  --config research\training-2b-xpu-distill-s1024.json `
  --base-dir models\decision-2b-distill-s1024\backbone `
  --data-dir research\data\typelogic-mix-v2 `
  --init-from models\decision-2b-distill-s1024 `
  --out research\runs\b580-xpu-smoke `
  --smoke
```

Only proceed to full training with `"smoke_passed": true` in `smoke.json` and memory below the limit.

## Training

```powershell
& $py -u tools\train_colab_v2.py `
  --device xpu `
  --config research\training-2b-xpu-distill-s1024.json `
  --base-dir models\decision-2b-distill-s1024\backbone `
  --data-dir research\data\typelogic-mix-v2 `
  --init-from models\decision-2b-distill-s1024 `
  --out research\runs\b580-xpu-mix-v2
```

To interrupt, stop after a checkpoint and resume the same folder with `--resume` (the resume file has a checksum). Do not reuse the folder for another dataset or configuration.

## Evaluate, calibrate and install

1. Evaluate on the GPU: `tools\evaluate_local.py --device xpu --run research\runs\b580-xpu-mix-v2 --base-dir models\decision-2b-distill-s1024\backbone --data-dir research\data\typelogic-mix-v2` (report with calibration and test logits).
2. Assemble a new profile without touching the active one: `tools\assemble_checkpoint.py --trained research\runs\b580-xpu-mix-v2 --base-dir models\decision-2b-distill-s1024\backbone --out models\decision-2b-mix-v2`.
3. Apply the temperature fitted on the calibration split: `tools\apply_calibration.py --report <report> --model-dir models\decision-2b-mix-v2`.
4. Compare with the current profile on the in-house tests (`tools\eval_split.py`) and on BANKING77. Only then activate it: `python tools\run_app.py profiles --use decision-2b-mix-v2`.
5. Update `MODEL-PARITY.json` with the new adapter and data (see [MODEL-PARITY-ANALYSIS.md](MODEL-PARITY-ANALYSIS.md)).

A successful training run does not certify quality: measure on cases from your domain and keep human review.
