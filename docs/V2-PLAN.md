# TypeLogic v2 plan

Status: **TypeLogic 2.0.0 released (2026-10-30); phases 0-4 done.** TypeLogic 1.x stays the
development toolchain (training, calibration, evaluation).

## Goal

The same decisions as 1.x, from a smaller and simpler installation:

- **One executable.** `TypeLogic.exe` is the launcher, the local web UI/API and the supervisor of the inference
  server. No Python, no Ollama, no `.bat` files at run time.
- **Only the models that earn their place.** Each model has one role and a measured reason to be installed.
- **Same invariants as 1.x.** Offline at run time, no telemetry, loopback only (`127.0.0.1`), never truncate
  silently, every model file pinned by revision and SHA-256, all UI and documentation in English.

## Key finding: llama.cpp serves the specialist natively

llama.cpp **v0.8.0** (released 2026-10-28, commit `8642541b`, Windows binaries published as build `b11960`) answers typed decisions through
`POST /v1/systemone` (the TypeSafe System One API) for a list of decision-model families. One of them, **`kev`**,
is exactly the architecture of the TypeLogic specialist (`typelogic/vendor/pointer.py` comes from Kev):

| TypeLogic specialist | llama.cpp `kev` |
|---|---|
| `<|fim_prefix|>` state, `<|fim_middle|>` question, `<|box_start|>` option `<|box_end|>`, `<|fim_suffix|>` decide | the same template (`conversion/lev.py`, `KevModel`) |
| each question is a causal row continuing the state (hybrid Gated DeltaNet backbone) | each question is one prompt sharing the state prefix (`can_share_prompt`) |
| `PointerHead`: `q, k = Linear(2048, 256)`, logits `k(h_opt) . q(h_decide) / sqrt(256)` / T | classifier `[q | k]` on the final hidden state, `dot / sqrt(n_pointer)`, softmax with the GGUF temperature |
| `<|x|>` in user text rewritten to `<¦x¦>` | `server_decision_escape_special_tokens`, same rewrite |
| noul options `no`, `yes: …`; choice `key: description`; score level text | the same option texts |

The official converter merges the LoRA (all 12 target modules, including the DeltaNet projections) into the
Qwen3.5-2B weights and stores the pointer head; `spike/convert_specialist.py` runs it offline against the backbone
that 1.x already has (every file checked against its SHA-256 provenance) and writes the calibrated temperature
(`calibration.json`, T = 1.399) into the GGUF.

Consequences:

- No custom inference code at all: no FFI to llama.cpp, no hidden-state readout, no Rust/ONNX/PyTorch head.
- The same server also runs **Clef-Flash** (`clef` family, `/v1/systemone`) and **Plumb** (text generation), in
  **router mode**: one `llama-server` process loads and unloads the models of a preset file on demand
  (`--models-preset`, `--models-max`, `--models-memory-margin`).
- GPU through **Vulkan** on the Intel Arc B580 (no oneAPI runtime). Gated DeltaNet and SSM-conv ops have Vulkan
  kernels in v0.8.0; a SYCL build is the fallback if Vulkan latency is poor.

## Architecture

```
TypeLogic.exe  (Rust standard library + typelogic-core, no third-party crates)
 ├─ web UI + API on 127.0.0.1:8765        (UI of 1.x, embedded)
 ├─ validation, exports, digests           (typelogic-core: same rules and messages as 1.x)
 ├─ library: decisions, tags, 200-limit archive
 └─ supervises ─► llama-server (llama.cpp v0.8.0, router mode, 127.0.0.1:<random>, --offline)
                   ├─ writer      plumb-4b-v5-Q4_K_M.gguf            /completion, chat      always (<= 16 options, titles)
                   ├─ clef        Cloudflare_clef-flash-IQ4_XS.gguf  /v1/systemone (clef)   > 16 options, when downloaded and the GPU has room
                   └─ specialist  typelogic-specialist-2b-Q8_0.gguf  /v1/systemone (kev)    > 16 options otherwise (small GPU, CPU)
```

- The child process is placed in a Windows job object: closing TypeLogic.exe always ends the inference server.
- `setup` downloads with Windows' own `curl.exe` and unpacks with `tar.exe` (both ship with Windows 10+), then checks
  SHA-256 in Rust: the application carries no TLS or HTTP-client library, and the run-time process never connects
  outside the loopback address.
- Python remains only on the development side (1.x repository): training, calibration, evaluation, conversion.
  Python's AI libraries keep improving there; v2 consumes their output as a GGUF file.

## Models and sizes (decided by the phase 0 spikes, see docs/SPIKE-RESULTS.md)

| Role | File | Size on disk | Device memory | Status |
|---|---|---|---|---|
| specialist | TypeLogic Qwen3.5-2B + LoRA, Q8_0 (head F16) | 1.88 GiB | 2.0 GiB with the router settings (patched llama.dll, output matrix in RAM; 5.5 GiB with the release DLL) | required: questions with more than 16 options (catalogs) |
| generalist + writer | Plumb-4B v5 Q4_K_M (Apache-2.0), direct option-letter readout and text | 2.52 GiB | 2.3 GiB with the router settings (8-bit KV cache, 1 slot; 3.6 GiB with defaults) | required: questions with up to 16 options, titles |
| extractor | Needle 3 (121M, 2-bit) | 36 MB | - | kept for short texts (phase 3) |
| runtime | llama.cpp v0.8.0 (build b11960) Windows Vulkan, llama.dll rebuilt with `spike/patches/qwen35-skip-lm-head.patch` until it is upstream | 36 MB (zip) | - | required |
| application | TypeLogic.exe | a few MB | - | required |

Routing is the 1.x rule: a question with at most 16 options goes to Plumb, a larger one to the specialist. Both fit in
the 12 GB of the B580 at the same time (about 4.4 GiB with the router settings).

Optional download: Clef-Flash IQ4_XS (bartowski, 5.0 GiB on disk, 5.7 GiB on the GPU; best calibrated together with
Plumb). The three models run at once on the B580 (10.1 GiB, about 1.1 GiB free, measured by `spike/trio.py`) with the
same answers as each model alone, see docs/SPIKE-RESULTS.md. Not installed: the ggml-org Clef-Flash Q4_K_M (6.9 GiB on
the GPU, does not fit beside the other two), the specialist in Q4_K_M
(fails G2), Plumb Q8_0 (same accuracy as Q4_K_M), Clef 27B, the colibri gateway (Python), Ollama, PyTorch, the
qwen3.5:4b extractor, ONNX Runtime.

| Installation | Models | Total on disk |
|---|---|---|
| **v2 default** | specialist Q8 + Plumb Q4 | **~4.4 GiB** |
| v2 + optional Clef-Flash | + Clef-Flash IQ4_XS | ~9.4 GiB |
| 1.x today | PyTorch backbone, runtime, Ollama models | ~14-19 GB |

## Phases and acceptance criteria

### Phase 0 - spikes (this folder, `spike/`)

`spike/run-spikes.bat` converts the specialist, fetches the pinned files and measures every target on the B580.
Gates (computed by `spike/report.py`):

- **G1 parity:** the specialist GGUF Q8_0 picks the same answer as the PyTorch engine of 1.x on >= 99% of the
  JevBench (231) and test2 (615) questions.
- **G2 small specialist:** Q4_K_M loses <= 1.0 accuracy point against Q8_0 on JevBench, test2 and BANKING77.
- **G3 small writer:** Plumb Q4_K_M loses <= 1.0 point against Q8_0 (direct readout) on JevBench and test2.
- **G4 generalist:** Clef-Flash beats Plumb Q8 on chance-corrected skill on JevBench and test2; otherwise the
  generalist role is dropped (scenario A).

If G1 fails, the difference is located first (tokenization of the state, temperature, option texts, quantization:
F16 vs Q8 separates conversion from quantization) before anything else is built.

### Phase 1 - runtime

`TypeLogic setup | serve | doctor`, router supervision, `POST /v1/systemone` (validated with typelogic-core, 1.x
messages), `GET /api/health`, an MCP server (stdio, one `decide` tool), the two-permutation order diagnostic of 1.x.
Accept: JevBench through `TypeLogic.exe` reproduces the spike numbers exactly; the executable has no third-party
crate; nothing listens outside 127.0.0.1 (test); the inference server never outlives the application (test).

**Status: done (2026-10-29), measured by `spike\run-spikes.bat app` on the B580, see docs/SPIKE-RESULTS.md.**

- `TypeLogic setup [--with-clef] [--from DIR]`: runtime and models from models.lock.json (SHA-256 checked, hard links
  from local folders, otherwise `curl`/`tar` of Windows). The specialist GGUF and the patched llama.dll have no public
  URL yet; a release must publish them (phase 4).
- `POST /api/evaluate`: 1.x contract and answers (typelogic-core), routing of `typelogic/hybrid.py` with Clef-Flash
  first for catalogs, Plumb read in Rust (prompt byte-identical to 1.x, checked on 61 prompts), order diagnostic.
- Models chosen at every start from the free GPU memory (`layout::choose`): Plumb + Clef-Flash (>= 8.7 GiB free),
  specialist + Plumb (>= 4.9 GiB), otherwise both on the CPU; `TYPELOGIC_MODELS` / `TYPELOGIC_DEVICE` override.
  The router's own memory admission is off (`--models-memory-margin 0`): it counts the LM head of decision models
  and refused the three models together although they fit.
- `TypeLogic mcp`: MCP server (stdio), tool `decide`, through a running TypeLogic or its own models.

### Phase 2 - application

The 1.x web UI (TypeScript, already built to static files) served from the executable; the library (title, tags,
30 per page, 200-decision limit with the archive to `data/DD-MM-YYYY.rar`, zip fallback through `tar.exe`); exports
(Lite, digests: already in typelogic-core); templates; titles from the writer model or the heuristic of 1.x.
Accept: the 1.x UI test suite (`web/tests/ui.test.mjs`) passes against the Rust server.

**Status: done (2026-10-29).** `web/tests/ui.test.mjs` (headless Chromium, the deterministic engine of
`cargo build --features fixture`): 23/23 checks pass against TypeLogic.exe. `spike\run-spikes.bat ui` on the B580 with
the real models: 14/14 checks (template case 2.4 s with a title written by Plumb, evidence extraction by Plumb 1.2 s,
a 77-option catalog answered by Clef-Flash, library, exports, CPU only mode loaded in 10 s and back).

- The library is one JSON file per decision in `data/library` (no SQLite in the executable), with the SHA-512 / MD5
  of 1.x; `TypeLogic import typelogic-export.json` (or `POST /api/import`) takes the library of 1.x (Export all).
- Titles and evidence extraction use Plumb through llama-server (JSON constrained to the schema); without Plumb the
  title comes from the case text as in 1.x. The ports of `casefmt`, `titles` and the evidence check are compared with
  the Python outputs (tests/gen/*.py: 100 requests, 278 titles, 555 checks).
- Archives: `data/archives/DD-MM-YYYY.rar` with WinRAR, otherwise a .zip written by TypeLogic (stored entries,
  tested before the decisions are removed).
- Engine & privacy offers Automatic or CPU only; the mode change restarts llama-server with the new models.
- Needle 3 (short-text extraction) came in phase 3.

### Phase 3 - quality

Hybrid decisions (specialist x generalist, the product-of-experts rule of 1.x), chance-corrected skill in the
evaluation views, a Decision Index run through `/v1/systemone`, bias probes (option order, position), Needle through
its C ABI for short-text extraction.

**Status: done (2026-10-30).** `spike\run-spikes.bat phase3` on the B580 (Plumb + Clef-Flash loaded): 13/13 checks;
`web/tests/ui.test.mjs`: 26/26.

- **Combined mode** (Engine & privacy > Route / Combine, `/v1/decide?combine=ensemble`): questions with up to 16
  options are read by Plumb and by Clef-Flash (w = 0.5) or the specialist (w = 0.55), `p ~ plumb^w * other^(1-w)`
  as in 1.x; answers carry `engine: ensemble` and both components. Against the product of experts computed from the
  trio runs measured alone: JevBench 87.9% vs 87.4%, Brier 0.176 vs 0.178; test2 88.6% vs 88.5%, Brier 0.170 vs
  0.169 (one changed answer each, GPU arithmetic). Routed Plumb alone on test2: 85.9%, Brier 0.210.
- **Evaluate page**: the 231 public JevBench items (embedded) or the user's JSONL, run in the background, saved in
  `data/evaluations`. The **Decision Index** is JevBench's Intelligence axis on the public tiers: per-tier
  chance-corrected accuracy, clipped at 0, weighted hard 30 / standard (original) 28 / easy 14; failures count as
  wrong. It is not the leaderboard score (no held-out items, no judge tier, no calibration or cost axis).

  | JevBench public, B580 | Decision Index | accuracy | Brier | ECE | hard tier | 231 items |
  |---|---|---|---|---|---|---|
  | Route (Plumb, order probe on) | 83.3 | 87.9% | 0.193 | 0.050 | 78.4% | 175 s |
  | Combine (Plumb x Clef-Flash) | 83.5 | 87.9% | 0.176 | 0.038 | 77.5% | 236 s |
  | Clef-Flash only | 75.6 | 81.8% | 0.251 | 0.053 | 64.9% | 226 s |

  The Evaluate page and `/v1/decide` give the same routed accuracy (0.8788 both).
- **Bias probes**: option order (Plumb changed its top choice on 21% of 139 Choice questions when the options were
  rotated; Clef-Flash on none), first / last option chosen vs gold (within 5 points), Yes polarity (+5 to +8 points
  more Yes than gold), Score leniency (+0.08 to +0.21 levels).
- **Needle 3** through its C ABI (`LoadLibraryExW` / `dlopen`, no crate): the DLL and weights are hash-checked against
  the `needle` entry of `config/models.lock.json` before loading; `setup` installs them (from `--from` folders or the
  pinned wheel, only the engine file taken out). Same fields as 1.x on the same text; 1.9 s on the first call
  (load), 0.14 s after. `/api/extract`, evidence extraction with backend `needle` (texts up to 1,500 characters) and
  `extracted_fields` on a decision (every answer flagged `model_extracted_fields`) work as in 1.x. Needle itself
  misses some values (no total came back for 1,250.40 EUR; its reasoning mentions 12.40), in 1.x as here: its
  values stay unverified suggestions.

### Phase 4 - release

One archive: `TypeLogic.exe`, `runtime/llama/`, `config/`; models fetched by `setup` from `config/models.lock.json`.

**Status: published (2026-10-30), https://github.com/marcusbsilva/Typelogic-AI/releases/tag/v2.0.0.** `tools/package.py` writes
`release/TypeLogic-2.0.0-windows-x64.zip` (32 MiB: TypeLogic.exe, the llama.cpp runtime with the patched
llama.dll, config, licenses) and the release assets (specialist GGUF, llama.dll, SHA256SUMS.txt).
`spike\run-spikes.bat release`: 10/10 on a clean folder (see docs/VERIFICATION.md).

- First start: a double-click on a fresh copy lists the missing models, asks before downloading (Clef-Flash when
  the GPU has room) and runs setup; without a console to ask, nothing is downloaded.
- The specialist GGUF and llama.dll are published as assets of the GitHub release v2.0.0 (URLs in
  `config/models.lock.json`); TypeLogic 1.x moves to the `v1` branch.

## Known limits of the llama.cpp path

- A Score question has at most **10 levels** through `/v1/systemone` (1.x accepted 32). `TypeLogic.exe` reports it
  as a validation error; no 1.x template uses more than 4.
- A server instance that runs Clef only serves `/v1/systemone` (no text generation): titles then come from the
  writer model or the heuristic.
- For Kev and Clef, a question with its options must fit in one micro-batch (`--ubatch-size 4096` in the presets).
- The specialist was trained with states of up to 1024 tokens; longer states are answered but not validated.
