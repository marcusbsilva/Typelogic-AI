# Evidence for a llama.cpp change: skip the LM head of Qwen3.5 classifier models in embeddings mode

Raw facts and numbers for the contributor. This is not a PR description: llama.cpp does not accept AI-written PR
descriptions or automated submissions (AGENTS.md, CONTRIBUTING.md section "AI Usage Policy"). Write the PR in your own
words and disclose AI usage in the template.

## Where

- `src/models/qwen35.cpp`, end of `llama_model_qwen35::graph::graph`. Same at tag v0.8.0 (8642541b) and at master
  cf24317 (2026-10-28).
- After the `model.cls_out` block, the LM head is always built: `cur = build_lora_mm(model.output, cur, ...)`,
  `res->t_logits = cur`.
- In embeddings mode every token is an output (`llama_context::decode`: `output_all = cparams.embeddings`), so the graph
  computes `n_vocab` (248,320) logits per prompt token. `graph_has_logits` is then true, so the host logits buffer is
  also sized `n_vocab * n_outputs`.
- The server never reads them for a Kev decision model: `send_decision` reads `llama_get_embeddings_ith` only
  (pointer at the last token, markers at `<|box_end|>`).
- Precedent: `src/models/lfm2.cpp` builds the LM head only `if (!cparams.embeddings)`.

## Change

`qwen35-skip-lm-head.patch` (5 lines): return after the classifier projection when `cparams.embeddings` is true.
Models without `cls_out` and all text generation (`cparams.embeddings == false`) are unchanged.

## Reproduction

1. Convert a Kev checkpoint (`conversion/lev.py`, `KevModel`) or download `ggml-org/Kev-4B-GGUF`.
2. `llama-server -m <kev.gguf> -ngl 999 -c 16384 -b 3072 -ub 3072 -np 2 -lv 4`
3. Look at `compute buffer size` in the log, then send `POST /v1/systemone` requests and compare latency.

## Measurements

GPU: Intel Arc B580 12 GB, Vulkan, Windows, release `llama-b11960-bin-win-vulkan-x64.zip`; patched run = same release
with only `llama.dll` rebuilt (MSVC 19.51) from 8642541b + patch. Model: Qwen3.5-2B Kev checkpoint, Q8_0, classifier in
F16, `-ub 3072 -np 2 -c 16384`.

| | release | patched |
|---|---|---|
| device memory after load | 5,581 MiB | 2,671 MiB |
| compute buffer (Vulkan0) | 3,438 MiB | 528 MiB |
| median latency, 231 short items (JevBench) | 0.059 s | 0.045 s |
| median latency, 213 records of 2-5 questions, about 1k-token states | 0.415 s | 0.254 s |
| median latency, 1000 items with 77 options | 0.488 s | 0.295 s |
| answers | - | identical on 1,846 questions: largest probability difference 0, no changed argmax |

CPU check (Linux, 2 cores, Kev-4B Q4_K_M, `-ub 2048`, 20 items): compute buffer 2,092 -> 348 MiB, total time
48.9 -> 42.5 s, largest probability difference 0.0.

Charts: `kev-memory.png`, `kev-latency.png`.

## Checks to do before opening the PR

- Build with the patch on master, not only on v0.8.0.
- Text generation with a plain Qwen3.5 GGUF (no `cls_out`) still works: `llama-cli` or `/completion`.
- A Qwen3.5 model with `cls_out` used through `/v1/systemone` and `/embedding` gives the same output as before.
- `test-backend-ops` is not needed (no ggml change); `llama-perplexity` does not apply to this embeddings-only path.

## Context

Found while porting TypeLogic AI (https://github.com/marcusbsilva/TypeLogic-AI) to a llama.cpp-based runtime for its
next version.
