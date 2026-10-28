# Research: what other developers are doing (October 2026)

Survey carried out on 2026-10-24 on GitHub, Hugging Face, JevBench, forums and Reddit. The Reddit pages did not open directly: one data point came from a mirror and is marked as such. The claims come from the cited repositories themselves; what was not verified is indicated.

## LAYA (original project)

- **Current version:** 0.4.14 (2026-10-23). There is no official "LAYA 2.0"; our LAYA 2.0 document describes an architecture of our own. New in 0.4.14: better English routing, a Go SDK and stricter calibration checks. https://github.com/NandhaKishorM/laya
- **Model:** it is an encoder (ModernBERT-large, 421M), trained with RLCD (rewards from its own scoring rules). It answers in about 33 ms on a T4.
- **Option order:** order rotations reduced the answers that change with order from 16.3% to 6.1%. A "parallel" layout makes the answer 100% order-invariant, but accuracy drops from 0.779 to 0.764.
- **Large catalogs:** on BANKING77, tournament mode raised accuracy from 0.430 to 0.610, and embedding pre-selection (bge-small) reached 0.630.
- **Limits the project itself declares:**
  - it is close to chance on typed decisions without training (0.36);
  - it truncates above 20 options;
  - it gets negations wrong (issue #377);
  - in `noul`, it follows the labels instead of the text (#156);
  - it has position bias in `score` (#131);
  - it is overconfident without calibration;
  - it cuts context at 512 tokens, which helps explain its 54.4 score on JevBench.
- **What TypeLogic already handles:**
  - it reads catalogs of 151 options in one pass (91.8%);
  - it uses states of up to 2,048 tokens in the specialist and 4,096 in the generalist;
  - it checks option order in both engines;
  - it calibrates per backend.

## Open Jev-type models at the top of JevBench

| Project | Recipe | What is relevant for us |
|---|---|---|
| **Plumb-4B** (crh225/plumb, Apache-2.0) | Qwen3.5-4B with the SemIf readout. A Qwen3.8-27B writes long documents and hard questions; there is 1:1 replay of human-labeled data and mining of errors. Trains with cross-entropy on the option letters on a 16 GB GPU. | It is the same format as our generalist. It has an Ollama build (`crh225/plumb-4b`, Q8_0, built-in temperature 1.75) through the `/v1/systemone` endpoint, which uses the same request format as TypeLogic. 89.6% on the public JevBench. |
| **Imajev-4B** (mohit67890/imajev, Apache-2.0) | Rank-16 LoRA also on the DeltaNet projections; about 1M decisions filtered by agreement between two orders and between 2–3 teachers; soft labels; weight averaging. | Confirms our case: training on classification alone made the 9B worse on hard questions (64.9% → 42.3%), and only teacher-written hard questions recovered it. This is the pattern we saw in the specialist (91.8% on BANKING77, 59.3% on JevBench). |
| **JevK5** (allebee/jevk5, Apache-2.0) | Distilled from Qwen3.6-27B and GPT-6 Luna; has a GGUF for `llama-server` and a 2B version. | Possible starting point and GGUF alternative. |
| **kev** (jaredpalmer/kev) | LoRA plus pointer head, like our specialist. Fine-tuning from the already trained checkpoint gave 0.83 on their evaluation set, against 0.33 starting from the base model. | Adopted: the trainer gained `--init-from` to resume from the trained adapter. |
| **reflex** (kshetrajna12/reflex) | Says that every fine-tune got worse on long, ambiguous inputs; what helped was the readout: yes/no with letters, "Evidence / Criterion" framing and two-order averaging, which cuts ECE in half. | Our prompt already uses evidence/criterion. Two-order averaging is the next calibration improvement. |

**Caution:** JevBench v1.4 added 308 sealed items, and on them almost all single-pass models are only slightly above chance (29.3%), Jev included: Jev 36.7%, Plumb 38.0%, Imajev 37.0%. These items require multi-step reasoning. That is why we use the public JevBench only as a reference and choose checkpoints on our own test (`research/data/typelogic-distill-v1/test.jsonl`). https://github.com/fstandhartinger/jevbench

## Performance on Intel Arc

- **flash-linear-attention** now installs for Intel GPUs (`pip install flash-linear-attention[xpu]`) and removes the slow Gated DeltaNet fallback. Not verified on native Windows, which depends on Triton XPU. https://github.com/fla-org/flash-linear-attention
- **llama.cpp SYCL:** on an Arc Pro B60 (Battlemage, the same generation as the B580), Qwen3.5-9B went from 329 to 1,634 tokens/s of prefill (pp2048) between builds 8175 and 8688. Prefill is exactly the cost of direct readout. Vulkan, which Ollama uses on the test machine, still has the DeltaNet chunked kernel under review (PR #30207). https://forum.level1techs.com/t/arc-pro-b60-for-local-llms/246971/13
- **IPEX is discontinued** (March 2026); the path forward is native PyTorch XPU, which we already use.
- **Memory during training:** bitsandbytes has a build for Intel GPUs, and Unsloth documents QLoRA on Intel on Windows 11 (with Qwen3, not Qwen3.5). Not tested here.
- **Reddit (via mirror):** a B580 with SYCL ran Qwen3.6-35B-A3B at 400 tokens/s of prefill (pp512) and 29.7 tokens/s of generation (tg128).

## Distillation datasets (2026 practices)

- **Consensus:** keep only items on which 2–3 teachers agree, or on which two option orders agree with a minimum confidence. Imajev kept 9,368 of 13,386 questions this way.
- **Soft labels and mining:** train on the teachers' distributions and mine the current model's errors, with 1:1 replay of real data.
- **Decontamination:** discard documents that share 8-word sequences with the benchmark.
- **Ready-made datasets** (not verified here): Eikos decisions (about 23.6 thousand rows, CC-BY-4.0) and LocalLLaMA/typed-decisions.

## Actions for TypeLogic

| # | Action | Status |
|---|---|---|
| 1 | Own test, outside JevBench | Done: `typelogic-distill-v1` (144 test questions) and `typelogic-distill-v1-test2` (615 questions, documents generated later and without overlap) |
| 2 | Compare ready-made open models in Ollama | Done: Plumb-4B became the default generalist (87.9% on the public JevBench, 88.9% on our own test) |
| 3 | Retrain the specialist with hard teacher questions, consensus, BANKING77 replay and resumption from the checkpoint | Done: profile `decision-2b-distill-s1024`. Own test 2 rose from 69.9% to 79.2% and BANKING77 held (91.80% to 91.85%). |
| 4 | Two-order option averaging | Done and optional (`TYPELOGIC_DIRECT_ORDERS=2`): +0.8 to +1.4 points, better NLL and Brier, 2× latency (see `docs/HYBRID-ENGINE.md`) |
| 5 | Per-question-type temperature | Tested and rejected: it worsens the test with the available calibration data |
| 6 | llama.cpp SYCL build and `flash-linear-attention[xpu]` | Not applied in this version. The generalist already runs in 0.21 s through Ollama/Vulkan. `fla[xpu]` depends on Triton XPU on Windows, which was not verified, and installing it in the training environment would have put the ongoing training at risk. |
| 7 | Embedding pre-selection for catalogs with more than 16 options | Not adopted. The specialist reads catalogs of up to 151 options with 91.8% on BANKING77, while LAYA's pre-selection reaches 63%. It would also require an additional embedding model. |
| 8 | Combine specialist and generalist on questions with up to 16 options | Done and optional (the Hybrid combination option Ensemble: Engine & privacy page, `TypeLogic.bat start --mode hybrid --ensemble`, or `TYPELOGIC_HYBRID_MODE=ensemble`): same accuracy, 16% lower NLL and 20% lower Brier on test 2 |

## Distillation dataset built (2026-10-24)

- **Generation:** long documents (policy + case facts) with 3–5 questions each, in English and Portuguese, written by OpenRouter models.
- **Labeling:** four open teachers read each question with the same direct-readout prompt as the generalist: Qwen3.6-35B-A3B, Qwen3.8-Flash, DeepSeek-V4.1-Flash and Gemma-4-26B-A4B. The two Qwen models return log-probabilities.
- **Jev:** a sample of 79 questions was read by `typesafe/jev-router`, which routed to GLM-5.3, GPT-6, Gemini 4, DeepSeek, Claude and Grok. It agrees with the kept consensus on 63 of 66 questions (95%).
- **Agreement of each teacher with the consensus:** 93% to 95%.
- **Consensus:** 2,985 of 3,456 questions kept. Another 615 questions were set aside for the extra test.
- **Decontamination:** documents with more than two 8-word sequences in common with JevBench were discarded.
- **Total OpenRouter spend:** US$3.80 of US$4.26 (89%). The remaining balance is US$0.46.
