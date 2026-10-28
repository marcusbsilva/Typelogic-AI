# Hybrid engine: trained specialist + generalist with direct readout

Version 1.1. Measured on a test machine on 2026-10-24: Ryzen 5 8600G, 16 GB of RAM, Intel Arc B580 12 GB.

## Summary

TypeLogic has two engines and picks one per question, before running any model:

| Engine | What it is | Where it is better |
|---|---|---|
| **Specialist** | Qwen3.5-2B trained on the B580 (LoRA + decision head), profile `decision-2b-distill-s1024` since 1.1. It reads any number of options in one pass and is calibrated for the training domain. | Large catalogs. On the BANKING77 test, with 77 to 151 options per question, it scores **91.85%**. |
| **Generalist** | An instruction model without a trained head, with direct readout: Plumb-4B through Ollama by default (also Qwen3.5-9B or 4B). The model receives state, criterion and lettered options, and the answer is the softmax of the letter probabilities, in a single step and without generating text. This is the SemIf technique (MIT). | Rules, policies, judgment and multiple steps, with up to 16 options. On the public JevBench it scores **87.9%** with Plumb-4B, 84.0% with Qwen3.5-9B and 79.7% with Qwen3.5-4B, against 62.8% for the specialist. |

**Routing rule:** questions with more than 16 options go to the specialist; the rest go to the generalist. Each answer records in the `engine` field which engine produced it. To force a single engine: `TYPELOGIC_ROUTE=specialist`.

## Results on the public JevBench

The data are the 231 public items of v1.2, MIT licensed, in `research/data/jevbench-public`. The tool is `tools/jevbench_eval.py`. The full results are in `research/results/jevbench/`, except for the two rows marked †: they come from an earlier measurement whose raw files were not kept (for Qwen3.5-4B in PyTorch only the 72 original items are saved, `direct-4b-cpu-bf16-original`, 97.2%). Measure them again with `tools/jevbench_eval.py` before relying on them; the README uses the Ollama 4B row, which has its files.

| System (test machine) | Accuracy | Original (72) | Easy (48) | Hard (111) | Brier | ECE | p50 latency |
|---|---|---|---|---|---|---|---|
| Specialist 2B, state up to 2,048 tokens | 59.3% | 80.6% | 97.9% | 28.8% (36 long items rejected) | 0.435 | 16.9% | 1.6 s (XPU fp32) |
| Specialist 2B, state up to 4,096 tokens | — | — | — | 36.0% | — | — | 0.25 s (XPU fp16) |
| Direct readout Qwen3.5-2B (base, untrained) † | 65.8% | 75.0% | 100% | 45.0% | 0.477 | 15.2% | 0.08 s (XPU bf16) |
| **Direct readout Qwen3.5-4B, PyTorch bf16** † | **79.7%** | 97.2% | 100% | 59.5% | 0.253 | 5.4% | 0.11 s (XPU) · 1.8 s (CPU bf16) |
| **Direct readout Qwen3.5-4B, Ollama GGUF Q4** | **79.2%** | 95.8% | 97.9% | 60.4% | 0.274 | 4.4% | 0.19 s (B580 via Vulkan) |
| **Direct readout Qwen3.5-9B, Ollama GGUF** | **84.0%** | 95.8% | 100% | 69.4% | 0.240 | 5.7% | 0.26 s (B580 via Vulkan) |
| **Plumb-4B (crh225/plumb-4b, Apache-2.0), Ollama Q8, same direct readout** | **87.9%** | 97.2% | 100% | 76.6% | 0.189 | 3.0% | 0.21 s (B580 via Vulkan) |

References published by JevBench on the same 231 items, computed from `jevbench-v1.2-per-task.json`:

| System | Accuracy |
|---|---|
| Jev 1.13 (TypeSafe) | 86.6% |
| SemIf (Qwen3.5-4B, same technique) | 81.0% |
| Open-Jev 9B | 77.5% |
| Open-Jev 2B | 64.5% |
| Laya 421M | 58.4% |

**Conclusions:**
- **Reproduction validated.** The 4B direct readout reproduces SemIf: 184 against 187 correct answers.
- **GGUF.** The Ollama Q4 version loses only 1 correct answer (183) and agrees with PyTorch on 217 of the 231 items.
- **Qwen3.5-9B.** Qwen3.5-9B through Ollama (6.6 GB) reaches 84.0%, above SemIf (81.0%) and close to Jev (86.6%).
- **Plumb-4B.** It is a Qwen3.5-4B trained by another developer specifically for this readout, with long documents, teacher consensus and replay of real data (see `docs/RESEARCH-2026-10.md`). On the test machine, through Ollama, it scored **87.9%**: above Jev on the public items, with a 4B model, already good calibration (ECE 3.0%) and 0.21 s per question. It became the default generalist of Hybrid mode (`TypeLogic.bat start --mode hybrid`), with the specialist on the CPU or on the B580. The Ollama `/v1/systemone` endpoint rejected the model ("not a llama.cpp decision model"), but direct readout in raw mode works, because Plumb was trained with the same prompt as SemIf. Combining 4B and 9B does not improve the result: the best is still the 194 correct answers of the 9B alone.
- **Specialist.** It is weak on general questions, but it remains the only engine that reads catalogs of 77 or more options in a single pass.

## Analysis of the cited repositories

| Repository | What it is | What we use from it |
|---|---|---|
| `TheoLeeCJ/SemIf` (cited in `Meanblock/JEV-CPU`) | Direct readout of the option letters with an instruction model; PyTorch, llama.cpp and MLX backends. | The technique, with credit and MIT license (`typelogic/direct.py`), and the noul/choice/score mapping. Our additional Ollama backend reuses models already installed in the local Ollama. |
| `Meanblock/JEV-CPU` | CPU version of SemIf with Qwen3-0.6B. | Only the idea of running without a GPU. The 0.6B is weak (0.44 balanced accuracy in SemIf), and our 4B runs on the CPU in bf16 at 1.8 s per question. |
| `AlephFunk/jev-qwen` | LoRA adapters for Qwen3.5-4B-Base reading the letters " A", " B"…; the V1c variant uses Brier loss. | The Brier loss and the calibration criterion. The adapters were not integrated: they require the 4B-Base, and their own README says they do not meet their reliability rule. |
| `ZefanCai/Open-Jev-9B` | LoRA plus scalar head on Qwen3.5-9B; open data (CC0, 113 thousand rows). | The model does not fit on a 16 GB machine. The data were tested for calibrating the generalist: the game and painting tasks are synthetic and not usable (the 4B scores 26 to 36% on them, and the temperature fitted on the whole set worsened the ECE on JevBench from 5.8% to 29%). We use only the `workflow-controls` and `customer-control` subsets. |
| `jsaurabh/qwen3.5-9b-jev-data-mix-v2` | LoRA on Qwen3.5-9B with candidate readout; 79.65% on the public JevBench. | The public JevBench as an independent test. Its 9B does not fit to run in bf16. |
| `mikim869/qwen2.5-3b-jev-decision-engine` | Qwen2.5-3B that answers by generating JSON text. | Nothing: generating text is exactly what TypeLogic avoids, and the repository publishes no results. |

### 4B model and GGUF

- **Training the 4B on the B580:** it does not fit. Training the 2B already uses 8.75 GiB of VRAM, and the 4B in fp16 takes about 8 GB for the weights alone. That is why the 4B is used untrained, as the generalist.
- **GGUF / Ollama:** feasible and measured. Ollama 0.40 returns up to 20 log-probabilities per position, and the option letters always appeared among them (0 of 231 cases needed the floor). The prompt is built by the reference tokenizer and sent in `raw` mode.
- **Specialist in GGUF:** not feasible. It depends on its own positions and attention masks and on reading the hidden states of several positions, which llama.cpp/Ollama do not expose.

## How to use

The engine mode is chosen in the web app (Engine & privacy page: CPU, GPU or Hybrid), or with `--mode cpu|gpu|hybrid` on the command line or the launcher, and is saved in `settings.json` in the data folder.

| Command | Purpose |
|---|---|
| `TypeLogic.bat models` | Downloads and verifies the models: specialist base, adapter, Qwen3.5-4B tokenizer (enough for Ollama; on its own: `TypeLogic.bat models --only tokenizer`) and Plumb-4B through Ollama. `TypeLogic.bat models --only weights-4b` also downloads the Qwen3.5-4B weights (about 9 GB) for the generalist on the CPU. |
| `ollama pull crh225/plumb-4b` | Recommended generalist: 5.2 GB GGUF Q8, runs on the GPU through Ollama. Alternatives: `qwen3.5:9b`, or `qwen3.5:4b` at 2.6 GB. |
| `TypeLogic.bat start --mode hybrid` | The specialist runs on the CPU (bfloat16) and the generalist uses Ollama; the app starts the local Ollama server by itself in hybrid mode. The CPU fallback (Qwen3.5-4B in bf16) exists only when the 4B weights are in `models\qwen35-4b`. |
| `TypeLogic.bat setup --gpu` + `TypeLogic.bat start --mode hybrid --hybrid-device gpu` | The specialist runs on the B580 in fp16 (0.14 s per short question, 4.5 GB of VRAM) and the generalist uses Ollama. |
| `TypeLogic.bat verify --hybrid` | Includes real decisions and the curated examples going through the hybrid engine. |

**Variables:**

| Variable | Function |
|---|---|
| `TYPELOGIC_MODE` | `cpu`, `gpu` or `hybrid`; overrides the saved choice. |
| `TYPELOGIC_GENERALIST` | Generalist folder. |
| `TYPELOGIC_OLLAMA_MODEL` | Model tag in Ollama. |
| `TYPELOGIC_GENERALIST_DEVICE` | `cpu` or `xpu`. |
| `TYPELOGIC_GENERALIST_FALLBACK` | `cpu` (default) or `none`. |
| `TYPELOGIC_XPU_PRECISION` | `float16` (default) or `float32`. |
| `TYPELOGIC_THREADS` | CPU threads. |
| `TYPELOGIC_DIRECT_ORDERS` | `1` (default) or `2`: the generalist also reads each question with the options in reverse order and averages the two. |

**Calibration:** temperature fitted on the `workflow-controls` and `customer-control` subsets of Open-Jev (1,200 rows, CC0):

| Backend | Temperature | ECE on the Open-Jev test |
|---|---|---|
| 4B PyTorch | 1.4 | 7.7% → 7.4% |
| **9B Ollama** | **1.7** | **11.2% → 7.1%** |

The independent check on JevBench shows different effects:
- **9B:** temperature 1.7 improves the result. NLL drops from 0.453 to about 0.42 and ECE from 5.7% to about 4%.
- **4B:** temperature 1.4 leaves NLL almost unchanged (0.483 → 0.478) and ECE goes from 5.4% to 6.7%. The difference is within the noise of 231 items.

The generalist uses `direct-calibration-<backend>[-<model>].json` inside the model folder. A temperature fitted on one backend is never reused on another. The tool is `tools/calibrate_direct.py`.

## Own test, two-order averaging and per-type temperature

The public JevBench is small, and other models have already been tuned on it. That is why TypeLogic now has its own test: `research/data/typelogic-distill-v1/test.jsonl` (52 long documents in English and Portuguese, 144 questions, see `docs/RESEARCH-2026-10.md`). The tool is `tools/eval_split.py`. Results of Plumb-4B on the test machine, through Ollama:

| Plumb-4B readout | Public JevBench (231) | Own test (144) | NLL / Brier (own test) | ECE (own test) | Latency |
|---|---|---|---|---|---|
| One order (default) | 87.9% | 88.9% | 0.397 / 0.116 | 3.7% | 0.21 s per question |
| **Two-order average** (`TYPELOGIC_DIRECT_ORDERS=2`) | **88.7%** | **90.3%** | **0.369 / 0.105** | 5.7% | about 2× |
| Previous specialist 2B (catalog v2), for comparison | 59.3% | 72.9% (6 questions with state above 2,048 tokens count as errors) | 0.786 / 0.305 | 10.6% | 0.3 s per document (XPU fp16; 10 s in a measurement taken with RAM under pressure) |
| New specialist 2B (`decision-2b-distill-s1024`) | 62.8% | 81.3% (the same 6 questions count as errors) | 0.493 / 0.173 | 6.7% | 0.3 s per document (XPU fp16) |

- **Two-order average.** The idea comes from the *reflex* project: each question is also read with the options in reverse order, and the two distributions are summed option by option. It improved accuracy, NLL and Brier on both tests, but the difference is within the margin of error and latency doubles. That is why it is optional. The temperature fitted for this readout on our own calibration set was 1.0, stored in `direct-calibration-ollama-crh225_plumb-4b-avg2.json`. For choice questions with the order check enabled, the reverse readout is reused, and the extra cost drops to zero.
- **Per-question-type temperature (noul, choice, score): rejected.** With 30 to 60 rows per type, it improved the calibration set and worsened the test. Without averaging: ECE from 3.7% to 5.9%. With averaging: NLL against the most probable label from 0.274 to 0.305. Plumb comes out of Ollama already well calibrated (fitted temperature 1.15 with one order, 1.0 with averaging), and no new temperature was adopted for one order.

## Combining the two engines (Hybrid combination option Ensemble)

On own test 2 the two engines make mistakes on different questions:

| Result | Questions |
|---|---|
| Both correct | 451 |
| Only Plumb-4B correct | 87 |
| Only the specialist correct | 39 |
| Both wrong | 38 |

An oracle that always picked the right engine would reach 93.8%. To measure how much of this can be used, the per-question answers of both engines were recorded (`eval_split.py --dump`) and combined with `tools/compare_engines.py`. The weight and the threshold were chosen only on the calibration and test splits of `typelogic-distill-v1` (288 questions), and the result was measured once on test 2 (615 questions):

| Readout (test 2) | Accuracy | NLL | Brier | ECE |
|---|---|---|---|---|
| New specialist | 79.7% | 0.550 | 0.208 | 6.7% |
| Plumb-4B, one order | 87.5% | 0.452 | 0.141 | 3.7% |
| Plumb-4B, two orders | 87.0% | 0.423 | 0.130 | 3.8% |
| Weighted average (Plumb weight 0.75) | 87.6% | 0.395 | 0.119 | 3.5% |
| **Weighted product (Plumb weight 0.55)** | 87.5% | **0.380** | **0.112** | **3.1%** |
| Plumb with the specialist only when Plumb is uncertain | 87.6% | 0.395 | 0.119 | 3.5% (the chosen threshold was "always combine") |

**Conclusions:**
- **Accuracy:** no combination changes accuracy measurably; the 95% interval of the difference from Plumb alone goes from -1.5 to +1.5 points.
- **Probabilities:** the combination improves the probabilities. NLL drops 16% and Brier 20%, meaning the displayed confidence is closer to reality. This helps decide what to review first.
- **Optional mode:** the Ensemble option uses the weighted product with weight 0.55 and is off by default, for three reasons:
  - It requires the specialist to be loaded together with the generalist.
  - On the B580, the two together with long documents exceeded the 12 GB of VRAM in one measurement.
  - On the CPU, each question then costs the specialist's time. The first real decision including loading took 88 s, against 10 s with the generalist alone.
- **How to turn it on:** select the Hybrid combination option Ensemble (Engine & privacy page, `TypeLogic.bat start --mode hybrid --ensemble`, or TYPELOGIC_HYBRID_MODE=ensemble); the weight is changed with `TYPELOGIC_ENSEMBLE_WEIGHT`. Each combined answer shows the two original distributions in `components`. If the specialist cannot read the question (state too long), the generalist answers alone and the answer records the reason.

## Retrained specialist (version 1.1)

The specialist was retrained on the B580 starting from the previous adapter (`--init-from`, an idea from the *kev* project). The data were `typelogic-distill-v1` with a 1:1 replay of BANKING77 (`research/data/typelogic-mix-v1-s1024`, 1,128 training rows).

- **Configuration:** 2 epochs, LoRA rank 16, lr 1e-5, head 1e-4, fp16, state of up to 1,024 tokens.
- **Why 1,024 tokens:** with 1,536, the card's memory spilled into RAM (11.7 GB + 5.4 GB shared) and training nearly stalled. The job now measures peak memory in a short test before choosing the state size. With 1,024, the peak was 11.2 GB and each step took about 1 minute.
- **Duration and checkpoint:** 282 steps, about 4 hours. Epoch 1 won: validation from 87.7% to 92.1%. Epoch 2 dropped to 91.3% and was discarded.
- **Interruption:** training was interrupted once by a Windows blue screen (`FAULTY_HARDWARE_CORRUPTED_PAGE`, a memory error on the test machine) and resumed from the checkpoint with an integrity check.

| Test | Previous specialist | **New specialist** |
|---|---|---|
| BANKING77, 8,260 questions with 77 to 151 options | 91.80% (7,583) · calibrated NLL 0.344 · ECE 2.9% | **91.85% (7,587)** · calibrated NLL **0.333** · ECE **2.2%** |
| Own test 1 (144 questions) | 72.9% | **81.3%** |
| Own test 2 (615 questions, generated after training) | 69.9% | **79.2%** (choice 74.7%, noul 87.8%, score 76.8%; English 79.8%, Portuguese 79.2%) |
| Public JevBench (231; 36 items with state above 2,048 tokens count as errors) | 59.3% | **62.8%** |
| Mix test (407 questions: distill + BANKING77) | — | 89.4% |

**Generalist on own test 2:** with Ollama running, Plumb-4B scored **87.5%** (NLL 0.452, ECE 3.7%, 1.2 s per document). In an earlier measurement Ollama was not running after the test machine restarted, and the generalist automatically fell back to Qwen3.5-4B through PyTorch on the CPU (bf16, temperature 1.4). Even so it scored **86.5%** with one order (NLL 0.417, ECE 6.3%) and **86.7%** with the two-order average (NLL 0.396, ECE 3.3%), at 25 to 50 s per document. That measurement also shows that the CPU fallback works. Plumb-4B was not measured in that test.

The new profile became the active one. Its temperature is 1.40, fitted on the BANKING77 calibration split and stored in `models/decision-2b-distill-s1024/calibration.json`.

## Limits

- The public JevBench has 231 items: the margin of error is about ±5 points. The tasks are in English.
- The generalist reads at most 16 options per question and was not trained on your domain. Measure it on labeled cases of your own before relying on its answers.
- The specialist remains the engine calibrated for intent catalogs. With long states (above 384 tokens, the training limit), its quality drops.
