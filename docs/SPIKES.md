# Phase 0 spikes

Three questions decide the shape of v2:

1. Does the specialist give the same answers in llama.cpp (GGUF, Vulkan) as in the PyTorch engine of 1.x?
2. How small can the specialist and the writer model be (Q8_0 or Q4_K_M) without losing accuracy?
3. Is Clef-Flash worth 6.7 GB as the generalist, compared with Plumb?

## Run

On the Windows computer with TypeLogic 1.x in `Downloads\TypeLogic-AI` and this folder in `Downloads\TypeLogic-V2`:

```bat
TypeLogic-V2\spike\run-spikes.bat
```

It uses the Python environments of 1.x (the GPU one, `%USERPROFILE%\TypeLogic-XPU`, when present) and installs
nothing. Steps, all resumable:

| Step | What happens | Network |
|---|---|---|
| fetch | llama.cpp v0.8.0 Windows Vulkan build into `runtime\llama`; Clef-Flash Q4_K_M, Plumb Q8_0 and Q4_K_M into `models\` (about 14 GB; a file already in the Ollama store with the same SHA-256 is copied) | GitHub, Hugging Face (pinned revisions, SHA-256 checked) |
| convert | specialist -> `typelogic-specialist-2b-F16.gguf` (llama.cpp `KevModel` converter, offline, backbone checked against its provenance), then Q8_0 and Q4_K_M with the pointer head kept in F16 | none |
| reference | PyTorch answers of the 1.x engine (same weights, calibration T = 1.399) | none |
| bench | each GGUF in `llama-server` on the GPU, one at a time; device memory from the load log | none (127.0.0.1) |
| report | `spike\results\REPORT.md` and `summary.json` | none |

Useful options: `--banking N` (BANKING77 rows, default 1000, 0 = all 2896), `--only spec-q8,clef-flash-q4`,
`--with-kev` (also the public Kev-4B for comparison), `--limit N` (smoke test), `--force` (measure again).

## Suites

| Suite | Questions | Gold | Source |
|---|---|---|---|
| JevBench | 231 (noul, choice, score) | `expected` | public JevBench, MIT |
| test2 | 615 in 213 records | argmax of the teacher target | held-out TypeLogic distillation set |
| BANKING77 | first 1000 test rows, 77 options | `gold_label` | intent-choice-en-v2, CC-BY-4.0 |

The direct readout of Plumb supports 2-16 options, so Plumb is not run on BANKING77.

## Gates

| Gate | Pass when | If it fails |
|---|---|---|
| G1 parity | specialist Q8_0 = PyTorch on >= 99% of JevBench and test2 questions | compare F16 with Q8_0 (conversion vs quantization), then state tokenization, temperature, option texts |
| G2 small specialist | Q4_K_M within 1.0 point of Q8_0 on every suite | ship Q8_0 (~2 GB) |
| G3 small writer | Plumb Q4_K_M within 1.0 point of Q8_0 on JevBench and test2 | ship Plumb Q8_0 |
| G4 generalist | Clef-Flash skill > Plumb Q8 skill on JevBench and test2 | no generalist decision model (scenario A of the plan) |

Skill is chance-corrected accuracy: `(correct - 1/k) / (1 - 1/k)` per question with `k` options, averaged; 0 is
guessing. A failed item (error, out of memory, over-long prompt) counts as a wrong answer.

## llama.cpp patch (step `llama-fix`)

`patches/qwen35-skip-lm-head.patch` (6 lines in `src/models/qwen35.cpp`) skips the LM head of a Qwen3.5 model that has
a classifier output (the Kev specialist) when it runs in embedding mode, as llama.cpp already does for LFM2. Measured on
the CPU with Kev-4B: identical answers (largest probability difference 0.0), compute buffer 2092 -> 348 MiB at a
2048-token micro-batch, 13% faster. On Windows:

```bat
run-spikes.bat llama-fix
run-spikes.bat bench --runtime fix --only spec-q8
run-spikes.bat report
```

`llama-fix` clones llama.cpp at the pinned commit (checked), applies the patch, builds only `llama.dll` with the Visual
Studio C++ tools and CMake, and writes `runtime\llama-fix` (a copy of the release with that one DLL replaced). The result
appears as `spec-q8-fix` in the report.

## Three models at once (step `trio`)

Alone, the three models need 2.6 + 3.6 + 6.9 GiB = 13.1 GiB; the B580 has 11.2 GiB free. `spike/trio.py` loads them
together with these settings and measures free memory, accuracy and a concurrent stress run:

| Setting | Where | Saves (estimate) |
|---|---|---|
| tied output matrix (`token_embd`) in system memory (`-ot token_embd\.weight=CPU`); with mmap it shares the pages of the input embedding, so it costs no extra RAM | all three | 515 + 497 + 546 MiB |
| LM-head patch (`llama-fix`) | specialist | 2,910 MiB (measured) |
| 1 slot, 8192-token context, 8-bit KV cache, flash attention, 512-token micro-batch | Plumb | about 700 MiB |
| prompt cache in RAM capped at 1 GiB (`--cache-ram 1024`, default 8 GiB) | Plumb | RAM |
| 12288-token context | specialist | 48 MiB |
| smaller Clef-Flash files (bartowski Q4_K_M 5.6 GiB, IQ4_XS 5.0 GiB) | Clef-Flash | up to 700 MiB, accuracy to be measured |

Latency: the Plumb client evaluates the prefix shared by all prompts of a request (system, evidence) once; llama-server
keeps a context checkpoint 4 tokens before the end of each prompt, so the following questions and option orders of a
hybrid (recurrent) model restart from that state instead of reading the evidence again.

```bat
run-spikes.bat fetch --with-clef-variants
run-spikes.bat trio
run-spikes.bat trio --clef clef-flash-q4km-b --trio-eval clef
run-spikes.bat trio --clef clef-flash-iq4xs-b --trio-eval clef
rem or all of the above in one run (resumes, skips files already measured):
run-spikes.bat trio-all
run-spikes.bat report
```

`report.py` then compares each model in the trio with the same model measured alone, question by question:

| Check | Pass when |
|---|---|
| specialist in the trio vs `spec-q8-fix` | no changed answer and no accuracy loss over 0.5 point |
| Plumb in the trio vs `plumb-q4` | accuracy loss <= 0.5 point (8-bit KV cache, LM head on the CPU) |
| Clef-Flash file in the trio vs `clef-flash-q4` alone | accuracy loss <= 1.0 point per suite |
| three models loaded | >= 256 MiB of device memory still free, no load error |
| concurrent stress | no error, every server alive |

A median latency more than 20% above the model alone is flagged "(slower)" but is not a failure.

## Memory note

In llama.cpp a decision model runs in embedding mode, where every prompt token is an output: the logits of the
whole vocabulary (248k tokens for Qwen3.5) are computed for each token of a micro-batch. Memory therefore grows with
the micro-batch size, and a Kev question with its options, or a whole Clef request, has to fit in one micro-batch.
The bench tries 3072 tokens for the specialist (4096 for Clef-Flash) and steps down when the GPU runs out of memory.
