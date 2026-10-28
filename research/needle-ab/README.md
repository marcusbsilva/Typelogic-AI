# Do extracted fields help the engines? (A/B test, 2026-10-26)

A test of whether attaching extracted fields to a decision (`extracted_fields`, see [docs/NEEDLE.md](../../docs/NEEDLE.md)) changes the answers of the TypeLogic engines.

## Setup

- **Sample:** 33 documents of `typelogic-distill-v1-test2` (20 English, 13 Portuguese). Together they hold 103 questions and 244 declared fields. The documents are fixed by seed 20261025.
- **Fields:** `design_fields.py` asked `google/gemma-4-26b-a4b-it` through OpenRouter, for each document, which 3 to 8 fields a reviewer would extract before answering its questions, and their values as written in the document.
  - It is an explicit research step that needs `OPENROUTER_API_KEY` and cost $0.007.
  - Of the 244 values, 240 passed the app's "written in the text" check. Those 240 are the **oracle** values: perfect extraction. They restate facts and never answer a question.
- **Runs:** `run_ab.py` (offline) sends every decision through `Application.evaluate` in five variants:

| Variant | What is attached |
|---|---|
| `none` | Nothing: the plain scenario |
| `format` | The fields with every value empty: same JSON wrapper, no information |
| `needle` | Values filled by Needle 3 on this computer |
| `oracle` | The oracle values |
| `corrupt` | The oracle values altered: numbers ×1.5, dates +7 days, texts swapped between documents |

- **Engines:** the generalist (Plumb-4B through Ollama, Intel Arc B580) and the specialist (Qwen3.5-2B adapter on the B580, float16). Windows 11, Ryzen 5 8600G.
- **Scoring:** answers are scored against the consensus targets with accuracy and cross-entropy (NLL). Differences against `none` are paired and come with a bootstrap 95% interval.

## Results

Needle extraction on these documents (`needle-extraction.json`):
- 136 of 244 fields were filled; 32 were equal to the oracle (13%).
- About 15 s per document.
- An earlier run on the same computer filled 128 fields, 28 of them equal to the oracle, so repeated runs do not give identical output.
- In a Linux run, 63 of the wrong values occur elsewhere in the text, so the "written in the scenario" check cannot flag them.

| Engine | Variant | Accuracy | NLL | Fixed / broken vs `none` |
|---|---|---|---|---|
| Generalist | none | 90.3% | 0.419 | |
| | format | 92.2% | 0.427 | 2 / 0 |
| | needle | 90.3% | 0.432 | 1 / 1 |
| | oracle | 91.3% | 0.457 | 1 / 0 |
| | corrupt | 88.3% | 0.467 | 1 / 3 |
| Specialist | none | 73.8% | 0.559 | |
| | format | 75.7% | 0.550 | 5 / 3 |
| | needle | 75.7% | 0.557 | 5 / 3 |
| | oracle | 76.7% | 0.536 | 6 / 3 |
| | corrupt | 76.7% | 0.552 | 6 / 3 |

Every accuracy interval includes zero.

What the paired rows show (`results-*-rows.jsonl`):
- **Generalist.** The one question that the oracle fixed was also fixed by `format`. The empty wrapper alone changed 3 answers.
- **Specialist.** Every question fixed or broken by `needle` was also fixed or broken by `format`. `oracle` and `corrupt` both added the same single extra fix. The specialist reacted to the JSON wrapper, not to the values: wrong values scored like correct ones.

## Conclusion

On this sample, the facts in the fields did not change the answers of either engine. Perfect values helped no more than an empty wrapper or deliberately wrong values. Needle extraction was also poor on documents of this length.

A model of our own trained to extract these fields better would therefore not improve the decisions. The small effect of the wrapper itself (+2 answers on both engines, not significant) is a separate question. It can be tested on the 615 questions of the full test set without any extraction.

Run `run_ab.py` on Windows from the GPU environment. For the specialist on the B580, unload the Ollama model first; the script does this itself.
