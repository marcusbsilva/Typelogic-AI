# Changelog

All notable changes to TypeLogic AI. Dates are in YYYY-MM-DD.

## 1.1.0 — 2026-10-25

First public release on GitHub.

### Added
- **Case / Evidence / Decision requests** (`typelogic/casefmt.py`):
  - **Format:** one case text, typed evidence with provenance (template, typed, extracted; confirmed or unverified), attachments, and decisions in the Jev style: Yes / no (the question), Choice (numbered options, each with when to choose it) or Score (levels from lowest to highest). Decisions need no name; they are answered as `decision_1`, `decision_2`, ...
  - **What the engine reads:** plain text with evidence lines by default, or a canonical JSON object.
  - **Measured on the 615 held-out questions:** accuracy unchanged within the 95% interval for both engines (`research/case-format-ab`).
- **Decision templates** (`typelogic/templates.py`, `templates/`):
  - 14 built-in templates: the 11 examples, ticket routing, a tool-call safety check and lead scoring.
  - "Save as template" writes to `templates/custom/` in the project and to `~/.typelogic-ai/templates/`. CLI: `templates [--id]`.
- **Evidence extraction** (`typelogic/evidence.py`, "Extract evidence" on New decision, `cli extract --template`, `POST /api/extract-evidence`):
  - **Backends:** the local LLM in Ollama (`qwen3.5:4b` by default) with JSON-schema output and a context sized to the text, or Needle for texts up to 1,500 characters.
  - **Measured:** qwen3.5:4b matched 1,421 of 1,537 values on 213 documents. On 33 of them, qwen3.5:9b matched 224 of 240 and Needle 32 of 244.
  - **Confirmation:** every extracted value stays unverified until the user keeps it, and unconfirmed values are flagged on every answer.
- **Case titles and tags** (`typelogic/titles.py`, `casefmt.validate_title`, `casefmt.validate_tags`):
  - **Title:** optional. Without one, a short title is written after the evaluation by the local Ollama model when Ollama already runs with it pulled (the Hybrid generalist, then the extraction model), otherwise from the first sentence of the case; `title_source` records which. Ollama is never started for a title. CLI: `decide --title`.
  - **Tags:** up to 8 per decision, lowercase, stored in the request. 20 suggested tags (`casefmt.TAGS`; `GET /api/tags`, `cli tags`), any other tag accepted; the web field suggests while typing. Templates can carry `tags`. CLI: `decide --tag`, `list --tag`.
- **Library:** every decision in a grid of 30 cards per page with page buttons, a tag filter built from the tags in use (suggested tags first, then your own, plus Untagged and *+N more*), newest, oldest or lowest-confidence order, a detail view with Back, corrections, selection with bulk delete and an Export menu. `POST /api/delete` with `id` or `ids`; CLI `delete --id`.
- **Library limit and archive** (`typelogic/archive.py`): at most 200 decisions over all workspaces. When the Library is full, Evaluate asks first; then every decision is compressed into `<project>/data/DD-MM-YYYY.rar` (WinRAR's `Rar.exe`, `TYPELOGIC_RAR`) or a `.zip` without WinRAR, with one JSON file per decision and a SHA-256 manifest, tested, and only then removed from the Library. `GET`/`POST /api/archive` (`{"confirm": true}`), 409 `library_full` from `POST /api/evaluate`, CLI `archive [--yes]`.
- `tools/fetch_models.py --only extractor` pulls the extraction model through Ollama.
- **Engine modes chosen in the web app:** CPU (default), GPU (Intel Arc) and Hybrid, on the Engine & privacy page, with `--mode` or in the launcher; saved in `<data>/settings.json` (`typelogic/modes.py`). Switching builds the new engine and frees the old one; switching to GPU from the CPU environment restarts the app in the GPU environment through the launcher.
- **Launcher** (`TypeLogic.bat` / `typelogic.sh` -> `launcher/launcher.py`, standard library only) with a menu and the commands `setup [--gpu]`, `models`, `start --mode`, `verify`, `cli`, `settings`, `evaluate`, `diagnose`, `test`, `build-native`. On Windows one Ctrl+C stops the app and closes the launcher, with no tracebacks and no "Terminate batch job" question; 0 exits the menu directly. It replaces every other `.bat` file and the `scripts/` folder.
- **Hybrid engine** (`typelogic/hybrid.py`, `TYPELOGIC_ENGINE=hybrid`): questions with at most 16 options go to a generalist, larger catalogs to the trained specialist. The UI shows which engine answered each question.
- **Generalist** (`typelogic/direct.py`): direct option-letter readout adapted from SemIf (MIT), through the local Ollama (default `crh225/plumb-4b`), llama.cpp (GGUF) or PyTorch (Qwen3.5-4B on CPU or Intel Arc). Calibration per backend, model and option-order setting.
- **Optional ensemble** (`TYPELOGIC_HYBRID_MODE=ensemble`): weighted product of both engines for questions with up to 16 options. Same accuracy as the generalist alone on the held-out test, NLL 0.452 → 0.380.
- **Optional two-order averaging** for the generalist (`TYPELOGIC_DIRECT_ORDERS=2`).
- **Retrained specialist** `decision-2b-distill-s1024`: warm-started from the previous adapter and trained on an Intel Arc B580 with `typelogic-distill-v1` plus a BANKING77 replay. Held-out test 2: 69.9% → 79.2%; BANKING77: 91.80% → 91.85%.
- **Datasets** `research/data/typelogic-distill-v1` (1,032 long documents in English and Portuguese, 2,985 questions kept by four-teacher consensus, decontaminated against JevBench) and `typelogic-distill-v1-test2` (615 questions generated after training). Generation pipeline in `research/distill/`.
- **Training export** of recorded corrections, in the Library and in the CLI (`training-export`); `tools/build_training_mix.py --feedback` turns them into a training mix.
- **Model download tool** `tools/fetch_models.py` (`TypeLogic.bat models`): Qwen3.5-2B backbone, specialist adapter (release asset), Qwen3.5-4B tokenizer and Plumb-4B through Ollama, all verified against SHA-256 hashes.
- In Hybrid mode the app starts the local Ollama server when it is closed (`typelogic/ollama.py`; nothing is downloaded).
- Specialist on Intel Arc (`TYPELOGIC_DEVICE=xpu`, float16, about 0.14 s per short question).
- Tools: `eval_split.py`, `compare_engines.py`, `jevbench_eval.py`, `build_training_mix.py`; `calibrate_direct.py --orders --per-type`; `train_colab_v2.py --init-from`.
- Benchmark charts and new screenshots in the README; user guide `docs/GUIDE.md`.
- **Needle 3** (Cactus Compute, Apache-2.0; `typelogic/needle.py`, `docs/NEEDLE.md`) as the offline evidence extractor for short texts.
  - **Offline only:** only the native engine is loaded, after SHA-256 verification against `models/needle/manifest.json`. The `cactus-needle` Python package, with its telemetry and Hugging Face calls, is never used.
  - **Install:** `TypeLogic.bat models --only needle` (36 MB).
  - **Measurements:** `tools/evaluate_needle.py` and `research/needle-ab`.

### Changed
- The repository ships no weights, Python runtime or compiled binaries; `.gitignore` and `.gitattributes` keep hashed files byte-exact.
- Engine profile page shows the generalist, routing and generalist calibration.
- CLI reads and writes UTF-8 on Windows; `verify_install` compares probabilities with a tolerance.
- `TYPELOGIC_OLLAMA_URL` must point to this computer (127.0.0.1, localhost or ::1); other addresses are refused.
- The New decision page is organized as Case (template, title, tags and one text), Evidence (optional) and Decisions; blocks 1 to 3 collapse and expand, and blocks 1 to 4 have a help icon. The decision editor follows Jev: Yes / no, Choice and Score tabs (one label each, with a help icon), no decision names, Yes / no is only the question, and Choice options and Score levels are numbered from 0. Each answer carries `confidence`. The example picker became the template picker, and the Needle field panel was folded into Extract evidence.
- **Lite JSON** is `{title, tags, questions}` (it no longer has a status), in Python and the Rust core; the integrity scope text no longer mentions a status.
- Research folder reduced to the data, results and configurations used by the current release.
- All documentation is in English (documents renamed: `HYBRID-ENGINE.md`, `RESEARCH-2026-10.md`, `OPTIONAL-COMPONENTS.md`, `MODEL-PARITY-ANALYSIS.md`, `TRAINING-INTEL-ARC.md`, `VERIFICATION.md`, `ARCHITECTURE.md`); the Portuguese README and guide and the CLI's Portuguese example aliases were removed.

### Fixed
- About 15 defects found in a code review (hybrid engine, local database, CLI, interface, generalist), each with a test; see `docs/VERIFICATION.md`.
- `models/<profile>/backbone/asset-provenance.json` is shipped with the exact bytes of the trained profile, so the specialist manifest also verifies on Linux.

### Removed
- Historical tools, evidence files, `docs/history` and the earlier specialist profile; the legacy 0.8B adapter path.
- The optional external services of release 1.0, which the application did not need (see `docs/OPTIONAL-COMPONENTS.md`):
  - the Elixir/OTP control plane (`control/`);
  - the connectors to NATS, Redis, PostgreSQL and Qdrant (`typelogic/stack/`), the `laya-stack.json` configuration and the `TYPELOGIC_STACK` variable;
  - `tools/laya_stack.py`, the `TypeLogic.bat stack` command and the `POST /api/stack/reconcile` route; `GET /api/status` no longer has a `stack` block.
- **Local memory** (references, document import of PDF, Word and Excel files, search, retrieval into the case, `--rag-evidence`), the recent-decisions list and their routes (`/api/import`, `/api/import-file`, `/api/documents`, `/api/search`, `/api/recent`) and CLI commands (`import`, `search`). The document-reader dependencies (pypdf, openpyxl, xlrd, olefile, defusedxml) are no longer installed. Existing `documents` tables in old databases are left untouched and unused.
- **Evidence rules** and **exact checks** (decimal sums, date comparisons, digests), in Python, the Rust core and the Node.js and Lua bindings. Requests and templates that still carry `rules` or `exact_checks` are accepted and those keys are ignored; a `case` with `summary` and `details` becomes one text.
- The case summary field (the case is one text; the title is a separate optional field).
- The Evaluation & overview page (with its Decisions and Corrections counters and the report viewer); local evaluation runs from the launcher: `TypeLogic.bat evaluate`, menu option 12.
- The *True when* / *False when* fields of Yes / no decisions in the app.
- **Review statuses and review modes:** pending, approved and denied, Approve, Deny and Clear, manual and automatic review (`review_mode`), the per-decision `auto_review` rules and the rule preview (`review_rules`), the review queue, the status counters on New decision and the Candidate threshold field. Routes `POST /api/review` and `GET /api/queue`, CLI `review`, `queue`, `list --status` and `training-export --no-approved` are gone; training export holds corrections only. Requests and templates that still carry `auto_review`, `verdict` or `review_mode` are accepted and those keys are ignored. Databases from earlier versions drop the `status` column when opened (the digests never covered it, so records stay verified).

## 1.0.0 — 2026-10-23

- Rust core (validation, canonical JSON, checksums, exact checks, Lite, retrieval, rank fusion, router, decision-head readout), identical to the Python reference, with Python (PyO3), Node.js (N-API) and Lua 5.4 bindings.
- Web UI rewritten in strict TypeScript.
- Optional Elixir/OTP control plane and connectors to NATS, Redis, PostgreSQL and Qdrant (off by default).
- Qwen3.5-2B specialist with LoRA and pointer head; BANKING77 catalog training on an Intel Arc B580.
