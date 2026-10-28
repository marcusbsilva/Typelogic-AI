# Case / Evidence / Decision format and evidence extraction (measurements, 2026-10-26)

`run.py` measures two things offline on the held-out set `typelogic-distill-v1-test2`: how well the local evidence
extractors work, and whether sending the case with typed evidence changes the answers of the engines.

Setup:
- **Hardware:** Windows 11, Ryzen 5 8600G, Intel Arc B580.
- **Generalist:** Plumb-4B through Ollama.
- **Specialist:** Qwen3.5-2B adapter on the B580, float16.

Evidence fields and oracle values per document come from `../needle-ab/design_fields.py`: one teacher model,
`google/gemma-4-26b-a4b-it`, declared 3 to 8 fields per document and copied their values. Only values written in the
document were kept. Generating them cost $0.0067 for 33 documents and $0.039 for all 213. The oracle values are one
model's reading, so "correct" below means "agrees with that reading".

## Evidence extraction (`evidence.py`, local Ollama, JSON-schema output)

| Extractor | Documents | Values matching the oracle | Wrong | Omitted | Seconds per document |
|---|---|---|---|---|---|
| qwen3.5:4b | 33 | 225 of 240 (94%) | 13 | 2 | 2.0 |
| qwen3.5:9b | 33 | 224 of 240 (93%) | 12 | 4 | 3.0 |
| qwen3.5:4b | 213 (all) | 1,421 of 1,537 (92%) | 103 | 13 | 2.1 |
| Needle 3 (for reference, `../needle-ab`) | 33 | 32 of 244 (13%) | — | 108 | ~15 |

Per-document values: `extraction-qwen3.5_4b.json`, `extraction-qwen3.5_9b.json`. The 4B model is the default: it is as accurate as the 9B and faster.

## Does the format change the answers?

Variants, all sent through `Application.evaluate`:

| Variant | What the engine reads |
|---|---|
| `text` | The document as plain text (the previous format) |
| `case_text` | The default v2 rendering: a one-line summary, the document, no evidence |
| `case_text_extracted` | The same, plus "name: value" lines of evidence extracted by qwen3.5:4b |
| `case`, `case_oracle`, `case_extracted` | The same three as a canonical JSON object |
| `oracle_only` | The summary and oracle evidence, without the document |

Accuracy against the consensus targets, with the paired difference to `text` and its bootstrap 95% interval.

**All 213 documents, 615 questions** (`results-615.json`, `results-615-text.json`):

| Variant | Generalist | Specialist |
|---|---|---|
| text | 87.6% | 79.6% |
| case_text | 86.8% (−0.8, [−2.0, +0.2]) | 79.1% (−0.5, [−1.8, +0.8]) |
| case_text_extracted | 87.5% (−0.2, [−1.8, +1.5]) | 78.9% (−0.5, [−2.5, +1.5]) |
| case (JSON) | 87.2% (−0.5, [−1.6, +0.7]) | 78.0% (−1.6, [−3.6, +0.3]) |
| case_oracle (JSON) | 87.2% (−0.5, [−2.1, +1.1]) | 78.1% (−1.3, [−3.5, +0.7]) |
| case_extracted (JSON) | 87.8% (+0.2, [−1.5, +1.6]) | 78.1% (−1.3, [−3.2, +0.8]) |

**First sample, 33 documents and 103 questions** (`results-103.json`):

| Variant | Generalist | Specialist |
|---|---|---|
| text | 90.3% | 73.8% |
| case | 91.3% | 76.7% |
| case_oracle | 91.3% | 78.6% |
| case_extracted | 91.3% | 78.6% |
| oracle_only | 68.0% | 62.1% |

**Specialist refusals.** The specialist refused 1 to 5 documents (3 to 13 questions) whose state exceeded its 2,048-token limit; they are left out of the paired comparisons, never cut. Without evidence, the plain-text rendering kept 612 questions readable, the same as `text`, against 608 with JSON. With evidence, both renderings kept 602.

## Conclusions

- **The local LLM extracts evidence well:** about 92% of the values match. That makes extraction useful for review, for rules computed on the evidence, and for automatic review.
- **The 103-question sample was misleading.** Its gain for the specialist (+4.9 points) disappeared on the full 615 questions, where every difference is inside the 95% interval of zero. With the current models, neither the case layout nor the evidence improves accuracy.
- **The JSON rendering is slightly worse.** It cost the specialist 1.3 to 1.6 points (not significant) and more tokens, so plain text with evidence lines is the default `engine_input`.
- **Never drop the document.** Evidence alone, without the document, loses 12 to 22 points.
- **Retraining is not justified yet.** No measurement shows a gain to amplify. Generating evidence for the 1,032 training documents would also need teacher credits.

Run `run.py` from the GPU environment on Windows. It unloads the Ollama models before the specialist runs on the GPU.
