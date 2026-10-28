# Research: datasets, results and reproducibility

This folder holds what is needed to reproduce and extend the evaluations of TypeLogic AI. Nothing here is imported by the offline application at startup. Tools that call external APIs (`openrouter.py`, `distill/`) run only when you start them explicitly and read the key from the `OPENROUTER_API_KEY` environment variable; never commit a key.

## Contents

| Path | What it is | License |
|---|---|---|
| `data/jevbench-public/` | The 231 public JevBench v1.2 items (original, easy, hard), unchanged | MIT (see its `LICENSE`) |
| `data/typelogic-distill-v1/` | Synthetic typed decisions over long documents in English and Portuguese: 1,032 documents, 2,985 questions, splits by document ([card](data/typelogic-distill-v1/README.md)) | Apache-2.0; texts generated with third-party models |
| `data/typelogic-distill-v1-test2/` | Extra held-out test: 213 documents, 615 questions generated after training ([card](data/typelogic-distill-v1-test2/README.md)) | same |
| `data/typelogic-mix-v1-s1024/manifest.json` | Manifest of the training mix of the active specialist (the JSONL files are rebuilt with `tools/build_training_mix.py`) | |
| `data/raw/` | Official BANKING77 and CLINC150 files used by the intent import | CC BY 4.0 / CC BY 3.0 |
| `data/intent-import-v1/report.json` | Expected output hashes of `tools/import_intent_sources.py` (checked by a test) | |
| `data/regression/` | Bilingual regression cases for `tools/evaluate_bilingual.py` | |
| `exposure-snapshot/` | Earlier prepared splits used only to decontaminate the intent import | CC BY 4.0 / CC BY 3.0 derived |
| `sources/intent-sources.lock.json` | Pinned sources and hashes of the raw data | |
| `banking77-catalog-en-v2.json`, `pilot-jev-*.json` | BANKING77 intent catalog (criteria text) and review policies of the catalog tools | |
| `qwen35-2b-assets.json` | Pinned file list and SHA-256 of Qwen3.5-2B (used by `tools/fetch_models.py` and at load) | |
| `training-2b.json`, `training-2b-xpu.json` | Base training configurations (frozen by `MODEL-PARITY.json`) | |
| `training-2b-xpu-distill-s1024.json` | Configuration of the active specialist run | |
| `runs/b580-xpu-distill-s1024/checkpoint.json` | Training record of the active specialist (the adapter itself is a release asset) | |
| `results/jevbench/` | Per-item results and summaries on the public JevBench | |
| `results/split/` | Results on `typelogic-distill-v1` and `-test2`, per-question dumps, calibration fits and the engine combination study | |
| `results/training/` | Evaluation reports of the active specialist (BANKING77 and the training mix) | |
| `results/precision-*.json` | bfloat16 vs float32 agreement check of the specialist on the CPU | |
| `distill/` | Pipeline that generated `typelogic-distill-v1` ([README](distill/README.md)) | Apache-2.0 |
| `openrouter.py` | Budget-ledger client of the earlier teacher collection (kept for its tests) | Apache-2.0 |

Large files are not in Git. For retraining, download `typelogic-training-data-v1.zip` from the GitHub release and extract it in the project folder: it adds `data/intent-choice-en-v2/` (audited BANKING77 + CLINC150 intent splits) and `data/jev-banking77-catalog-v2-audited/` (replay training file), which `tools/build_training_mix.py` and `tools/evaluate_local.py` read. `data/intent-choice-en-v2` can also be rebuilt from `data/raw` with `tools/import_intent_sources.py` and the catalog tools.

## Reproduce the main numbers

With the models installed (see the README):

```sh
# Public JevBench, default generalist (Plumb-4B through Ollama)
python tools/jevbench_eval.py --system direct --model-dir models/qwen35-4b --ollama-model crh225/plumb-4b --out research/results/jevbench/my-plumb
# Specialist on the public JevBench
python tools/jevbench_eval.py --system typelogic --device cpu --out research/results/jevbench/my-specialist
# Held-out test 2, both engines, with per-question dumps
python tools/eval_split.py --split research/data/typelogic-distill-v1-test2/test.jsonl --system direct --model-dir models/qwen35-4b --ollama-model crh225/plumb-4b --dump research/results/split/per-question/my-plumb-test2.jsonl --out research/results/split/my-plumb-test2.json
python tools/eval_split.py --split research/data/typelogic-distill-v1-test2/test.jsonl --system typelogic --dump research/results/split/per-question/my-specialist-test2.jsonl --out research/results/split/my-specialist-test2.json
# Engine combination (weights chosen on typelogic-distill-v1 calibration + test, reported once on test2)
python tools/compare_engines.py --a research/results/split/per-question/specialist-distill-s1024-{split}.jsonl --b research/results/split/per-question/plumb-4b-{split}.jsonl --tune distill-calibration,distill-test --test test2 --out research/results/split/combination.json
# BANKING77 test with the specialist on an Intel Arc (needs the training-data release asset)
python tools/evaluate_local.py --device xpu --data-dir research/data/intent-choice-en-v2 --train-file research/data/jev-banking77-catalog-v2-audited/train.jsonl
```

Expect small differences on other hardware (floating point, quantization of the Ollama model). The public JevBench has 231 items: about ±5 points of sampling error.

## Methodology notes

- Splits are by document or group (`group_id`), never by question. Calibration and test splits are never used for weight updates or checkpoint selection.
- Temperatures are fitted on calibration splits only and checked on separate data; a temperature that does not improve the held-out split is rejected (for example per-question-type temperatures, see `docs/MOTOR-HIBRIDO.md`).
- Synthetic data was decontaminated against JevBench (8-word sequences). It is not evidence of quality in any real domain.
- Earlier research stages (0.8B and 4B studies, catalog audits) are summarized in `docs/RESEARCH-2026-10.md` and `docs/HYBRID-ENGINE.md`.
