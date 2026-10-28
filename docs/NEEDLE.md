# Evidence extraction with Needle 3

[Needle 3](https://github.com/cactus-compute/needle) is a 121M-parameter tool-calling model by Cactus Compute
(Apache-2.0). TypeLogic uses it as the offline evidence extractor for **short texts**: it fills the evidence fields of
a template from a short case text when Ollama is not available. For longer texts the local LLM in Ollama is used; in
the measurements below Needle matched 13% of the fields of one-to-three-page documents and the local `qwen3.5:4b`
matched 94% of the same values.

Extracted values are **unverified**. Code checks every value against the case text, and the answers of a decision with
unconfirmed extracted evidence are flagged until you keep or correct each value.

## How it runs offline

| Part | What TypeLogic does |
|---|---|
| Native engine | `libneedle3.dll` (Windows), `.so` (Linux) or `.dylib` (macOS), taken out of the pinned `cactus-needle` 3.3.3 wheel for your platform. It imports only the operating system and C runtime libraries: it has no socket, HTTP or DNS code. |
| Weights | `needle3.cact` (35 MB, 2-bit) from `Cactus-Compute/needle3` at revision `d6c7d6a2e9655505e0f8e23339356dd1705900eb`. |
| Verification | `models/needle/manifest.json` holds the SHA-256 of the weights, of every wheel and of the engine of each platform. The engine is loaded only after both files match. |
| Python binding | `typelogic/needle.py`, a small `ctypes` binding. Its signatures are adapted from `needle/__init__.py` of the Needle repository. |
| Not used | The `cactus-needle` Python package is never installed or imported. That package sends usage telemetry (`needle/_telemetry.py`), contacts Hugging Face whenever it loads a model (`agent/fetch.py`), and includes hosted-platform and OpenRouter clients. None of that code is in TypeLogic. `NEEDLE_TELEMETRY=0` and `DO_NOT_TRACK=1` are still set, in case a copy of the package is installed somewhere else on the computer. |
| Network | None at run time. `tools/fetch_models.py --only needle` (the setup step) is the only part that downloads, and it runs only when you ask. `tests/test_needle.py` fails if any TypeLogic module imports the package or a network library. |

## Install

```bat
TypeLogic.bat models --only needle
```

This downloads about 36 MB into `models/needle/`: the weights, the license and the engine for this computer. It is also
part of the default `TypeLogic.bat models`. `--needle-platform win_amd64` (or another tag in the manifest) fetches the
engine for another computer. `TypeLogic.bat diagnose` reports whether Needle is installed and whether its hashes match.

## Use

Needle is the short-text backend of **Extract evidence** on the New decision page. The page and its other backend, the
local LLM in Ollama, are described in the [guide](GUIDE.md#1-web-workflow).

| Extractor | When it is used |
|---|---|
| **Auto** (default) | The local LLM in Ollama (`qwen3.5:4b`) when it is installed; otherwise Needle |
| **Needle** | Always Needle, for case texts up to 1,500 characters; longer texts are refused with a message |

Each suggested value gets a check:

- **written in the case**: the value appears in the text. For numbers, `4,800.00`, `4.800,00` and small numbers written as words before a noun ("zero units") count.
- **not found in the case**: the value does not appear in the text. It may be misread, invented or computed (for example a relative date).
- **inferred**: true/false and enum fields, which cannot be found literally.
- **not stated**: the extractor left the field out.

Values stay marked *extracted* until you keep or correct them. "Written in the case" means the value occurs in the
text, not that it fills the right field.

**Command line.**

```bat
TypeLogic.bat cli extract --input message.txt --template ticket-routing --backend needle
```

**HTTP.**
- `GET /api/needle` returns the status and limits.
- `POST /api/extract-evidence {"text", "schema", "backend": "needle"}` returns the suggested evidence.
- The lower-level `POST /api/extract {"text", "fields"}` is still available.
- Requests that carry the earlier `extracted_fields` block are still accepted. The block enters the state as
  `model_extracted_fields` and every answer is flagged.

## Limits

- In Extract evidence: up to 1,500 characters and 12 fields. The lower-level `/api/extract` reads up to 20,000 characters. Longer text is refused with a message, never cut. Time grows with
  length: under 1 s for a short scenario once loaded (the first call on Windows took 3.7 s with loading and hash checks) and about 15 s for 20,000 characters on the Linux test machine.
- The engine keeps one global state, so extractions run one at a time.
- Fields are optional in the schema sent to the engine, so it leaves out what the text does not state. Making them
  required filled more stated fields, but invented values for 5 of 5 absent fields in the measurement below.
- Needle is tuned for tool calls with specific fields. Generic catch-all schemas (for example "every amount and date"
  as nested lists) gave poor results and are not offered.

## Measurements

`python tools/evaluate_needle.py` repeats these measurements. Results are in `research/results/needle/`
(`needle-eval-windows.json`, Windows 11 with Ryzen 5 8600G; `needle-eval-linux.json`, Linux x86-64). The samples are
small, so the numbers show tendencies, not guarantees.

**Extraction.** The set has 7 fictional scenarios: 5 shipped examples, an invoice and a Portuguese refund. They declare 31 fields: 26 are stated in the text and 5 are not.

| Schema | Platform | Stated fields correct | Wrong | Omitted | Absent fields left out | Invented |
|---|---|---|---|---|---|---|
| Optional fields (used) | Windows and Linux (same result) | 15 of 26 | 1, flagged | 10 | 4 of 5 | 1, not flagged |
| Every field required | Windows | 20 of 26 | 6, 2 flagged | 0 | 0 of 5 | 5, 4 flagged |
| Every field required | Linux | 18 of 26 | 2, flagged | 6 | 0 of 5 | 5, 4 flagged |

"Flagged" means the check marked the value as not found in the scenario or inferred. With optional fields the engine
left out every field of the RPG scenario (six numbers in one sentence) and misread "3 de outubro" as December. With
required fields it filled the RPG fields with wrong numbers that do occur in the text ("1", "10", "0"), so the check
could not flag them. The same weights give slightly different outputs on the two platforms.

**Effect on decisions (A/B test on 103 questions).** [research/needle-ab](../research/needle-ab/README.md) ran 33
documents of the held-out test (103 questions, 244 fields) through both engines on Windows with the Arc B580. Each
decision ran without fields, with empty fields, with Needle's values, with perfect values copied from the document, and
with deliberately wrong values.

- On these documents of one to three pages, Needle filled 136 of 244 fields, and only 32 matched the document (13%). It
  took about 15 s per document, and repeated runs did not give identical output.
- Neither engine's answers depended on the values. The questions that changed with perfect values also changed with
  empty fields; for the specialist, wrong values scored like correct ones.
- Generalist: 90.3% without fields, 92.2% with empty fields, 90.3% with Needle and 91.3% with perfect values.
- Specialist: 73.8%, 75.7%, 75.7% and 76.7%.
- Every difference is inside the 95% interval of zero.

Attaching extracted fields is therefore a way to show and review values, not a way to improve answers.

**Sentence vectors.** Needle also returns sentence vectors. TypeLogic does not use them: in an earlier measurement on
BANKING77 they ranked the correct intent first less often than simple hashed term vectors, and the engine reads only about
the first 650 characters of a text. The binding no longer exposes them.

## License

Needle 3, its engine and its weights are published by Cactus Compute under the Apache License 2.0
([licenses/needle-Apache-2.0.txt](../licenses/needle-Apache-2.0.txt)). The binding in `typelogic/needle.py` adapts the
`ctypes` signatures of the Needle repository and states this in its header.
