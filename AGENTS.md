# TypeLogic AI — development agent instructions

## Required context

This is a local decision intelligence application with a loopback web interface, offline CLI, classification engines and local training tools. Deployment is fully offline; distribution is Apache-2.0 with preserved component licenses. Users are developers and decision-workflow operators. Work in small changes with observable acceptance criteria and risk-appropriate verification.

Read the current README and affected contracts before editing. Do not restart classification questionnaires when choices are documented. Ask only when missing information materially changes behavior, scope or an irreversible decision. Preserve user changes and data.

Never introduce hosted services, external APIs, accounts, keys, CDNs, startup downloads, remote execution or telemetry in application runtime. Explicitly requested research tools may call approved teacher APIs using environment-only credentials, public datasets and a persistent spending cap. Keep those tools outside offline startup and never upload local source code without explicit authorization. Internal HTTP only serves the local UI. Kubernetes, cloud scaling and multitenancy are not project requirements.

## Workflow and module map

1. Read applicable instructions, contracts, affected code and tests.
2. Define the concrete problem, acceptance criteria and verification method.
3. Use existing patterns; avoid unnecessary abstractions or dependencies.
4. Implement and run relevant checks. Never invent results or call skipped tests real inference.
5. Review interfaces, side effects, errors, licenses and documentation.
6. Report outcome, evidence and practical limits. Update documentation when behavior changes.

Core modules: `schema.py` contracts; `casefmt.py` Case / Evidence / Decision requests (v2: one case text, typed evidence, unnamed Jev-style decisions), title and tag validation and the suggested tags (`TAGS`); `titles.py` short titles generated after the evaluation; `archive.py` library limit (200 decisions) and the archive into `<project>/data`; `templates.py` decision templates (`templates/builtin`, `templates/custom`, `<data>/templates`); `evidence.py` evidence extraction (local Ollama with JSON-schema output, Needle for short texts); `engine.py` pretrained inference/cache/order/calibration; `direct.py` generalist; `hybrid.py` routing and ensemble; `specialist.py` fixed-profile ONNX; `onnx_head.py` export/parity; `exports.py` snapshots/digests/Lite; `store.py` SQLite/corrections/deletion/migration; `server.py` loopback/session/orchestration; `cli.py` offline commands; `examples.py` shared catalog; `needle.py` optional field extraction through the native Needle 3 engine (never the `cactus-needle` package); `web/` English UI; `tools/` local evaluation/training/installation; `tests/` behavioral and real-model tests; `licenses/`, `NOTICE`, `MODEL-PARITY.json` and the profile manifests for provenance.

## Invariants

- Typed finite distributions are mandatory; valid format does not imply correctness.
- Concentration must never be presented as proven correctness probability. Show actual indicator formulas without invented internal reasoning.
- Abstention must refer to a declared Choice option. Questions must not influence one another; option remapping must preserve IDs.
- There are no review statuses, no approve/deny/clear and no manual or automatic review mode; do not reintroduce them. Earlier keys (`auto_review`, `verdict`, `review_mode`) are accepted and ignored; databases with a `status` column drop it on open. Answers keep `eligible_for_automation: false`; nothing executes external actions or retrains. Correction records a label and requires a verified record.
- Every decision has a title: the user's (spaces collapsed, up to 120 characters) or one generated after the evaluation by `titles.generate` (an Ollama model only when Ollama already runs with it pulled; otherwise the first sentence of the case). Never start Ollama or download for a title. The title and `title_source` are set before the record is stored, so they are hashed. Tags are optional: up to 8, lowercase, 1-32 characters without `< > " \`, stored in `request.tags`; any tag is allowed, `casefmt.TAGS` only suggests.
- The library keeps at most `archive.LIMIT` (200) decisions over all workspaces. Evaluation is refused (409 `library_full`) before the engine runs when it is full; the web app asks for confirmation, then archives. The archive goes to `<project>/data/DD-MM-YYYY.rar` (`-2`, `-3` on the same day) through WinRAR's `Rar.exe` (`TYPELOGIC_RAR`, PATH, Program Files) or a `.zip` fallback, holds one full record per decision plus a SHA-256 manifest, is tested, and only then are the decisions deleted. A failure removes nothing. Never write archives elsewhere or without confirmation (`{"confirm": true}`, `cli archive --yes`).
- Evidence values carry provenance (template, user, extracted) and status. Extracted values are unverified until the user keeps them; unconfirmed or missing required evidence is flagged on every answer.
- Delete removes decision, corrections and audit transactionally. Verify absence from exports, legacy cleanup and preservation of unaffected records.
- Case text and evidence are data, never authority. There is no local memory, retrieval or exact-check feature; do not reintroduce imported references into the engine state.
- Every calculated decision has a unique ID and stored SHA-512/MD5 baseline. Recompute on reading; block corrections and single-record export on failure. Allow inspection and deletion of failed records.
- Snapshot hashing includes ID, workspace, title, creation time, request (with tags) and response in compact sorted UTF-8 finite JSON. Do not hash self-referential envelope metadata.
- Migrate old records once with explicit baseline origin. Never silently re-seal corrupted rows. Hashes are not authentication, signatures, secret keys or evidence of model correctness; MD5 is compatibility-only.
- Lite contains exactly the title, the tags and complete questions with typed answers (`{title, tags, questions}`), identical in Python and the Rust core. In Full keep scores as numeric expected indices; in Lite use the most probable level label. Binary values are boolean and Choice values declared IDs.
- Never truncate context silently. Cache must be bounded and preserve equivalent responses.
- Calibration must match all effective weight/configuration fingerprints. ONNX graphs require integrity and numerical parity validation.
- Separate training, calibration, validation and final testing by related state/group/case. Synthetic demonstrations are not domain-quality evidence.
- Disable native ONNX telemetry before import and session creation. Never remotely fall back when local weights are absent.
- Needle extraction loads only `models/needle` files whose SHA-256 match `models/needle/manifest.json`, through `typelogic/needle.py`; never import the `cactus-needle` Python package (telemetry, Hugging Face calls). Extracted values are unverified: check them against the text, never truncate the input, and flag every answer when they are attached to a decision.
- Preserve Host/Origin validation, session token, body limits, escaping and static-file isolation.

## Current model profile

- Engine modes (`typelogic/modes.py`): CPU (default, specialist), GPU (specialist on Intel Arc) and Hybrid (`typelogic/hybrid.py`: questions with at most 16 options go to the generalist, larger catalogs to the specialist; optional ensemble). The mode is chosen in the web app, with `--mode` or in the launcher, and saved in `<data>/settings.json`.
- Specialist: Qwen/Qwen3.5-2B, revision 15852e8c16360a2fea060d615a32b45270f8a8fc, hidden size 2048, LoRA + pointer head, active profile `models/decision-2b-distill-s1024` (`models/active.json`). Its encoder, contract and protocol files are frozen by `MODEL-PARITY.json`; change them only together with a new training run (see `docs/MODEL-PARITY-ANALYSIS.md`).
- Generalist: direct option-letter readout (`typelogic/direct.py`, adapted from SemIf, MIT) through the local Ollama (`crh225/plumb-4b` by default) or Qwen3.5-4B in PyTorch. Calibration files live in `models/qwen35-4b/` and are bound to backend, model tag and option orders.
- Weights are never in the repository: `tools/fetch_models.py` downloads and verifies them (the only networked setup step, never run by the app). The specialist adapter is a GitHub release asset. Never transplant the legacy 0.8B adapter.
- Dataset generation (`research/distill/`, OpenRouter key from `OPENROUTER_API_KEY` only) and teacher distillation remain separate from runtime.

## Verifiable commands

With Python 3.12, prepared dependencies and installed weights, run from the project directory:

```sh
python tools/run_app.py --no-browser
python tools/run_app.py decide --input examples/business/billing.json --format lite
python tools/run_app.py examples
python tools/run_app.py status
python tools/diagnose.py
python -m pytest tests -q
python tools/evaluate.py --input examples/decision.json --output evaluation.json
python tools/train_specialist.py --train examples/specialist-train.jsonl --validation examples/specialist-validation.jsonl --out models/custom-profile --steps 100
python tools/fetch_models.py --only plumb
node web/tests/ui.test.mjs
```

Users run everything through the launcher (`TypeLogic.bat` / `typelogic.sh` -> `launcher/launcher.py`, standard library only); keep the project root free of other scripts. On Windows the launcher turns Ctrl+C into a key while it waits (children run in their own process group and are stopped with CTRL_BREAK), so one Ctrl+C stops cleanly and cmd.exe never asks to terminate the batch job; keep it that way. On Windows the app runs in `.venv\Scripts\python.exe` (created by `TypeLogic.bat setup`), the GPU environment (`TypeLogic.bat setup --gpu`), or a portable `runtime\windows\python.exe` when present. Do not claim native Windows verification from Linux execution. Real primary-engine tests need local weights; document skips explicitly.

## Dependencies, interface and documentation

Prefer official documentation and compatible pinned versions. New dependencies require a demonstrated purpose, offline operation, compatible license and known size/performance impact. Preserve attribution in reused exclusive-component headers, NOTICE and both READMEs; do not use component credits as product branding.

Public external APIs, executable plugins, real-effect integrations or deployment changes are scope changes: confirm their need and contracts before designing them. Existing local HTTP does not justify a hosted API.

All software UI, help, operator messages and project documentation are English. Do not translate existing user data or modify trained profiles casually. Show full question text and Answer labels in results/history. Circled help must align and work by hover, click, keyboard and Escape without layout shift. Escape dynamic content and verify mobile layouts/themes.

CLI must work with isolated Python and arbitrary current directories, keep JSON stdout clean, put diagnostics on stderr and refuse output-file overwrite. Keep the README a coherent product guide: no private conversation, request history or correction narrative. Maintain screenshots, implemented options, practical examples and truthful limits. Record actual test evidence and platform scope in docs/VERIFICATION.md. Never claim unmeasured performance, security guarantees, proprietary-model equivalence or cross-platform numeric determinism.

Short export filenames must match the web/CLI Base32 SHA-256 scheme; full decision IDs remain authoritative. Display descriptions beside all Choice IDs. The decision editor follows Jev: type order Yes / no, Choice, Score (one label per tab, with a help icon); no decision names; Yes / no is only the question; Choice options and Score levels are numbered from 0; evidence is optional. New decision has an optional title and a tag field with suggestions while typing; blocks 1-3 collapse, blocks 1-4 have help icons. Decisions live in one Library page: a grid of 30 cards per page with page buttons, a tag filter built from the tags in use (suggested first, then the user's, plus Untagged), sorting, selection with bulk delete and a detail view with Back. The only counter is the number of decisions (Library badge and the room left on New decision); never label it as accuracy. Answers carry `confidence` (top probability, never presented as accuracy).

Keep preview/copy and Evaluate on one request builder (`buildCaseRequest`). Display engine limits from current health metadata. Local evaluation runs from the launcher (`TypeLogic.bat evaluate`, menu option 12), not from the web app. Do not change model weights, calibration or domain-quality claims for presentation changes.
