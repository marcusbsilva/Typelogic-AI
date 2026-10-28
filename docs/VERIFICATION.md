# Verification

Record of what was actually run in each release, on which platform and with what result. Skipped tests are declared; Linux results without weights are not presented as real inference. Newest entries first.

> **Titles, tags, Library pages and archive; review statuses and review modes removed (2026-10-27/28; Windows, Ryzen 5 8600G, and Linux container).**
> - **Linux container:** Python suite 260 passed, 7 skipped, the 8 known environment failures (no torch: `test_qwen2b`, `test_rebuild`). New tests: titles typed, generated from the case text, written by a (mocked) Ollama model and falling back when it fails; tag normalization and limits; Lite `{title, tags, questions}`; delete one or many; an earlier database loses its discarded rows and its `status` column and stays verified; the library limit refuses an evaluation before the engine runs, the archive (ZIP path, and the `rar a`/`rar t` command line with a fake `rar`) holds every record with its corrections and audit plus a SHA-256 manifest, the decisions are removed only afterwards, and a failed archive removes nothing; CLI `delete`, `tags`, `list --tag`, `archive`; `review` and `queue` refused.
> - **Rust core:** rebuilt offline in the container from the vendored crates; `cargo test -p typelogic-core` 12 passed; the parity test compares Lite with tags against the Python reference.
> - **Interface test in headless Chromium:** 23/23, with steps for the tag field (suggestions, keyboard, own tags), collapsible blocks 1-3 and help on blocks 1-4, the generated and typed titles, the Library tag filter, 30 cards per page and the pager, bulk delete, and the archive dialog (Cancel, then Archive and continue, with a lowered limit and a `.zip` in a temporary folder).
> - **Windows:** `TypeLogic.bat build-native` rebuilt the Rust core and bindings (backend `rust`); `TypeLogic.bat test`: 267 passed, 8 skipped. WinRAR was found at `C:\Program Files (x86)\WinRAR\Rar.exe`; three decisions in a temporary project were archived into `data\28-10-2026.rar` (written and tested by `Rar.exe`) and removed from the library. With Ollama running, `crh225/plumb-4b` and `qwen3.5:4b` both wrote titles; without a model the title came from the case text. A real Hybrid decision through the CLI (`decide --mode hybrid --tag "smoke test"` on the request-integrity template, separate data folder) got the title "Server rejects unauthorized admin role change" from `crh225/plumb-4b`, the tags `security, fraud, smoke test`, a verified record, and was found again by `list --tag "smoke test"`.
> - **Benchmark figures re-checked:** every number in the README and `docs/HYBRID-ENGINE.md` was compared with `research/results/` (JevBench 231 items, test 2 with 615 questions and the specialist + Plumb combination, BANKING77 8,260 questions) and matches. Two JevBench rows (Qwen3.5-4B in PyTorch, 79.7%, and untrained Qwen3.5-2B, 65.8%) have no raw files and are now marked as an earlier measurement; the README chart and table use the Qwen3.5-4B Ollama run (79.2%), which has its files.

> **Library, numbered Choice options, confidence and rule preview, launcher Ctrl+C and local evaluation (2026-10-26; Windows, Ryzen 5 8600G, and Linux container).**
> - **Linux container:** Python suite 271 passed, 7 skipped, the 8 known environment failures (no torch). New tests: `confidence` and `review_rules` are stored with the decision; one Ctrl+C stops a launcher child cleanly (POSIX path). Interface test in Chromium: 19/19, rewritten for the Library (status tags, detail view and Back, selection bar, Export menu), the counters on New decision, numbered Choice options, the optional Evidence section and the rule box on each answer.
> - **Windows, launcher (`TypeLogic.bat`) in a real console, driven by a harness that types keys into it:**
>   - Menu, `0` + Enter: the console closed in 3.3 s with exit code 0, no question.
>   - Menu, Ctrl+C: closed in 3.3 s, no "Terminate batch job" question.
>   - `start --mode cpu`, one Ctrl+C while the engine was loading: "Stopping...", "TypeLogic AI stopped.", the port was free and the console closed, no traceback and no question. It took 16 s because the app waited for the model to finish loading; the app now closes without waiting (os._exit after closing the database, 3 s at most).
>   - Menu option 1, one Ctrl+C: the same, plus one `BrokenPipeError` traceback from an open browser connection; connection errors are no longer printed.
> - **Windows, final build:** `TypeLogic.bat test`: 279 passed, 7 skipped. The harness again: menu `0` and menu Ctrl+C closed in 3.3 s without a question; `start` stopped 3.6 s after one Ctrl+C during model loading (was 16 s), with no traceback, the port freed and the console closed.
> - **Fix found:** the first Windows run stopped halfway: the new POSIX Ctrl+C test sent `os.kill(pid, SIGINT)`, which on Windows terminates the test process. The test is now skipped on Windows.

> **Jev-style decision editor, threshold automatic review; local memory, rules and exact checks removed (2026-10-26; Windows, Ryzen 5 8600G, Intel Arc B580, and Linux container).**
> - **Linux container:** Python suite 269 passed, 7 skipped; the 8 known environment failures remain (no torch in the container). Interface test in Chromium: 19/19, including a new step that builds Yes / no, Choice and Score decisions, sets their thresholds, checks the request JSON and evaluates it. The Rust core crate passes its own tests (`cargo test`, 12 passed); the PyO3 binding cannot be built offline in the container.
> - **Windows, this build:**
>   - `TypeLogic.bat build-native` rebuilt the Rust core and the Python, Node.js and Lua bindings without the exact-check and retrieval functions; `core.backend()` reports `rust`.
>   - `TypeLogic.bat test`: 278 passed, 6 skipped.
>   - Real decisions with `cli decide --review-mode auto` on the built-in templates:
>     - `execution-safety`, CPU (specialist): yes 18%, at or below 20% → denied by the Yes / no rule.
>     - `lead-scoring`, Hybrid (generalist): score 2.26, at or above level 2 (*Ready to buy*) → approved by the Score rule.
>     - `ticket-routing`, Hybrid (generalist): `billing` 92.2%; the template has no rule, so the request stayed pending.
>   - The web interface was not exercised in a browser on Windows.

> **Case / Evidence / Decision, templates, review modes and request tabs (2026-10-26; Windows, Ryzen 5 8600G, Intel Arc B580, and Linux container).**
> - **Evidence extraction (`research/case-format-ab/run.py`):**
>   - qwen3.5:4b in Ollama matched 225 of 240 oracle values on 33 documents, at 2.0 s per document.
>   - qwen3.5:9b matched 224 of 240, at 3.0 s.
>   - On all 213 documents, qwen3.5:4b matched 1,421 of 1,537, at 2.1 s.
>   - Needle matched 32 of 244 on the same 33 documents.
> - **Format test, 615 questions:**
>   - Generalist: text 87.6%, case 86.8%, case + extracted evidence 87.5%.
>   - Specialist: 79.6%, 79.1%, 78.9%.
>   - The JSON rendering gave the specialist 78.0% to 78.1%.
>   - Every paired difference is inside the 95% interval of zero.
>   - The 103-question sample had suggested +4.9 points for the specialist; the full set did not confirm it.
>   - The specialist refused, without cutting, the 1 to 5 documents that exceeded 2,048 tokens.
> - **Decision:** plain text with evidence lines is the default engine input; no retraining.
> - **Linux container:**
>   - Python suite: the new tests for v2 requests, rules, auto review, statuses, bulk review, templates, settings, evidence extraction and the local-only Ollama URL pass. The 10 known environment failures remain: no torch, olefile or xlrd.
>   - Interface test in Chromium: 19/19. It covers templates, evidence extraction with Keep, bulk Deny and Clear between the tabs, automatic denial by a rule, deletion from every tab, dark theme and mobile width.
> - **Windows, this build:**
>   - `TypeLogic.bat test`: 310 passed, 9 skipped.
>   - `TypeLogic.bat diagnose` reports Needle verified and evidence extraction ready (Ollama running, qwen3.5:4b pulled).
>   - `cli extract --template ticket-routing` on the payout message returned `payout_failed_days` 3 (written in the case), `payment_related` and `chat_timeout_reported` true, and `urgency` high, all unverified, in 8.3 s including the model load.
>   - `cli decide --mode cpu --review-mode auto` on the `execution-safety` template ran the real specialist on the CPU and answered "not safe". The deny rule recorded the decision as denied, and `cli list --status denied` listed it.
>   - The web interface was not exercised in a browser on Windows.
> - **Fix found:** the launcher printed its command line on stdout, which corrupted JSON written by `TypeLogic.bat cli ... > file`. It now prints it on stderr.

> **Extracted fields A/B test (2026-10-26; Windows, Ryzen 5 8600G, Intel Arc B580).** `research/needle-ab/run_ab.py` ran 33 held-out documents (103 questions) in five variants: no fields, empty fields, Needle, perfect values and corrupted values.
> - **Generalist (Plumb-4B):** 90.3%, 92.2%, 90.3%, 91.3% and 88.3%.
> - **Specialist (B580, float16):** 73.8%, 75.7%, 75.7%, 76.7% and 76.7%.
> - **Paired analysis:** perfect values fixed nothing that empty fields did not, and the specialist scored corrupted values like correct ones. Every interval includes zero.
> - **Needle on these documents:** 32 of 244 fields matched the document, about 15 s per document, and repeated runs differed.
> - **Problem found:** the first specialist run failed with `UR_RESULT_ERROR_OUT_OF_DEVICE_MEMORY` because the Ollama model held the GPU memory. The script now unloads Ollama before the specialist runs on the GPU.
> - Details: [research/needle-ab/README.md](../research/needle-ab/README.md).

> **Needle 3 field extraction (2026-10-25; Windows, Ryzen 5 8600G, and Linux container).** Optional field extraction through the native Needle 3 engine (`typelogic/needle.py`).
> - **Network audit:** the native engines of all eight platforms in the pinned wheels contain no socket, HTTP, DNS or telemetry strings, and the Linux engine imports no network functions. The telemetry (`needle/_telemetry.py`, Supabase endpoint), the Hugging Face download registration on every load (`agent/fetch.py`), the hosted-platform client and the OpenRouter client are all in the `cactus-needle` Python package, which TypeLogic never installs or imports (`tests/test_needle.py` checks this).
> - **Windows:** `TypeLogic.bat models --only needle` downloaded and verified the weights, the license and `libneedle3.dll` taken out of the `win_amd64` wheel. `TypeLogic.bat cli extract --preset request` returned `received_amount` 0.01, `received_action` refund and `added_role` admin, all marked as written in the scenario, with `trusted_amount` left out (3.7 s including the load). `TypeLogic.bat diagnose` reported `hashes: verified`. `TypeLogic.bat test`: 284 passed, 9 skipped.
> - **`tools/evaluate_needle.py`** (Windows and Linux): with optional fields, 15 of 26 stated fields correct, 1 wrong (flagged), 10 omitted, 1 of 5 absent fields invented. Required fields filled more fields but invented all 5 absent ones, so the schema keeps fields optional. Needle sentence vectors ranked BANKING77 intents worse than the existing hashed vectors (top-1 21 vs 31), so local memory search is unchanged. Details: [NEEDLE.md](NEEDLE.md).
> - **Linux container:** 273 passed, 10 skipped, 10 failures only due to missing `torch`/`olefile`/`xlrd`; the real-engine test ran with the Linux engine while external sockets were blocked. Interface test in Chromium 17/17, including extraction, attaching the fields, the forced review and refusal after the scenario changes.
> - **Also changed:** `TYPELOGIC_OLLAMA_URL` must point to this computer. `typelogic/offline.py` is frozen by `MODEL-PARITY.json` and was not edited; Needle's opt-out variables are set in `typelogic/needle.py` and in the launcher instead.

> **Launcher and engine modes (2026-10-25; Windows, Ryzen 5 8600G, Intel Arc B580).** All `.bat` files except `TypeLogic.bat` were replaced by the launcher (`launcher/launcher.py`), and the engine mode became a choice in the web app.
> - **Mode switching through the real server, started by the launcher (`start --mode cpu`):** CPU ready (decision 3.6 s) → Hybrid (Ollama started by the app, three answers by the generalist) → GPU: the app saved the choice, exited with code 75 and the launcher restarted it in `TypeLogic-XPU` (`device: xpu`, float16; first decision 22–31 s with warm-up, then 0.30 s) → Hybrid with the specialist on the GPU (BANKING77 question with 77 options answered by the specialist on `xpu`: `card_arrival`) → CPU again inside the GPU environment (ONNX readout, decision 4.1 s).
> - **Found and fixed:** CPU mode inside a GPU environment without ONNX Runtime failed (`No module named 'onnx'`); the CPU specialist now falls back to the PyTorch readout, and `TypeLogic.bat setup --gpu` installs the application packages into an existing GPU environment.
> - **`TypeLogic.bat verify --hybrid`:** 256 passed, 5 skipped; decisions with the ONNX, Rust and PyTorch heads (largest difference 3.1×10⁻⁷), hybrid decisions, curated examples 22/23 on the specialist and 23/23 on the hybrid, `passed: true`. `TypeLogic.bat settings`, `diagnose`, `setup --gpu` and the menu were also run.
> - **Linux container:** 242 passed, 10 skipped, 10 failures only due to missing `torch`/`olefile`; interface test in Chromium 16/16 (including the engine mode selector).

> **GitHub package, clean install (2026-10-25; Windows, Ryzen 5 8600G, Intel Arc B580).** The repository zip (without weights, runtime or binaries) was extracted into a new folder and installed as a user would:
> - **`instalar.bat`:** created the `.venv` with the system Python 3.12 and installed all of `requirements.txt` (`torch==2.8.0+cpu` from the PyTorch index); `env.cmd` switched to using the `.venv`.
> - **`tools/fetch_models.py`:** Qwen3.5-2B base verified file by file (12/12, `complete=True`); adapter installed from the release zip and verified against `manifest.json`; Qwen3.5-4B tokenizer downloaded from Hugging Face; Plumb-4B found in Ollama.
> - **Tests with `TYPELOGIC_REAL_INFERENCE=1`:** 235 passed, 19 skipped, both with the new `.venv` and with the portable runtime (skipped: Rust core not built in the clean copy, NATS/Redis/PostgreSQL services not configured, `scipy`/`scikit-learn` missing, large training data and the demonstration specialist, which are not in the repository).
> - **`verify_install.py --hybrid`:** real decisions with the ONNX and PyTorch heads (difference 0), hybrid decisions through Plumb-4B, curated examples 23/23 on the specialist and 23/23 on the hybrid, `passed: true`. Without the Rust core, the Python reference was used automatically.
> - **CLI through the `.venv`:** `typelogic-cli.bat decide --format lite` answered the `billing` example with the specialist on the CPU.
> - **Linux (Ubuntu 24.04 container, without PyTorch):** 236 passed, 10 skipped, 10 failures only due to missing `torch`/`olefile`; interface test in Chromium 15/15; `tools/fetch_models.py` tested with fake zips (`tests/test_fetch_models.py`).
> - **Fix found by the test:** the adapter zip must contain every file listed in the manifest (including `adapter/README.md`); `fetch_models.py` now reads the list from the manifest itself.

> **Review and bug hunt (2026-10-25).**
> - **Static analysis:** `ruff` (pyflakes, bugbear and security), `mypy` on the server, database, CLI and engine modules, `tsc` on the interface, `cargo clippy` and `cargo test` on the Rust core (15 tests).
> - **Independent review:** an agent that did not write the code audited the server, database, CLI, engines, interface and tools. The server's security boundary held up under the tests: Host, Origin, token, static files, body limit, NaN and escaping in the interface.
> - **Defects fixed, each with a new test:**
>   - a lazy specialist with an error stalled the whole hybrid engine;
>   - a tampered record with invalid JSON broke all listings;
>   - older documents disappeared from search from the 257th on;
>   - `list` and `export --format lite` aborted on an invalid record;
>   - unbounded in-process short-term memory;
>   - a token with non-ASCII characters brought down the request;
>   - a decision id of the wrong type produced a 500 error;
>   - generalist calibration applied to other weights or another readout;
>   - ensemble calibration label;
>   - training rows from the same scene split between training and validation;
>   - list targets in `eval_split`;
>   - database query outside the lock in the server;
>   - unstable order of decisions with the same timestamp on Windows.
> - **Cleanup:** unused imports and variables were removed outside the frozen files.
> - **Tools removed** (they pointed to data or models that no longer exist): `audit_qwen_options.py`, `evaluate_qwen_intents.py`, `compare_local.py` and `install_legacy_models.py` (with `model-package.json`).
> - **New features:** review queue (interface, `/api/queue`, CLI `queue`) and export of reviews for training (interface, `/api/training-export`, CLI `training-export`, `export_feedback.py`, `build_training_mix.py --feedback`).
> - **Results:**
>   - **Windows, with `TYPELOGIC_REAL_INFERENCE=1`:** 250 passed, 5 skipped (NATS, Redis and PostgreSQL services not configured; `scipy` and `scikit-learn` missing).
>   - **`verify_install --hybrid`:** ONNX, Rust and PyTorch decisions identical (difference of up to 3.1×10⁻⁷); hybrid decisions ok; curated examples 22/23 on the specialist and 23/23 on the hybrid.
>   - **Interface (headless Chromium, Linux):** 15/15, including the review queue and the training JSONL download.
>   - **`MODEL-PARITY.json`:** unchanged; the 16 frozen files are intact.
>
> **Engine combination and Ollama (2026-10-25).** With Ollama open, Plumb-4B scored 87.5% on in-house test 2 (615 questions; 87.0% with two orders). The per-question answers of the two engines were combined with `tools/compare_engines.py`, with the weight chosen on the `typelogic-distill-v1` splits. On test 2, the weighted product (weight 0.55) kept 87.5% and lowered the NLL from 0.452 to 0.380, the Brier score from 0.141 to 0.112 and the ECE from 3.7% to 3.1%. Real decision with `TYPELOGIC_HYBRID_MODE=ensemble` (Plumb through Ollama and bf16 specialist on the CPU): 3 questions combined, 88 s including specialist loading. Tests with `TYPELOGIC_REAL_INFERENCE=1`: 248 passed, 5 skipped. Note: with Plumb loaded on the B580, the fp16 specialist with states of up to 2,048 tokens overflowed the VRAM (`UR_RESULT_ERROR_OUT_OF_DEVICE_MEMORY`). The jobs now unload Ollama through the local API before using the GPU.
>
> **Release 1.1 on Windows with the real weights (2026-10-24 to 2026-10-25; Ryzen 5 8600G, 15 GB RAM, Intel Arc B580).**
>
> - **Training:** new profile `decision-2b-distill-s1024`, resumed from `b580-xpu-catalog-v2-v1` with `train_colab_v2.py --init-from`. It ran 282 steps in fp16 on the B580, with a peak of 11.2 GiB and states of up to 1,024 tokens; with 1,536 tokens the VRAM overflowed into RAM. A Windows blue screen (`FAULTY_HARDWARE_CORRUPTED_PAGE`) interrupted training at step 32, and it resumed from the verified checkpoint. Epoch 1 won, with validation going from 87.7% to 92.1%.
> - **Evaluation (`evaluate_local.py`):**
>   - BANKING77: test 7,587/8,260 = 91.85% (previously 91.80%); temperature 1.40 fitted on the calibration split only; NLL 0.368 → 0.333; ECE 4.1% → 2.2%; validation reproduced with difference 0.
>   - Mix test: 89.4% (407 questions).
> - **In-house tests (`eval_split.py`):**
>   - test 1: 81.3% (previously 72.9%);
>   - test 2: 79.2% (previously 69.9%; 615 questions, generated after training).
> - **Public JevBench:**
>   - new specialist: 62.8% (previously 59.3%);
>   - Plumb-4B through Ollama: 87.9% with one order and 88.7% with two.
> - **Generalist without Ollama:** with Ollama closed, the generalist fell back to Qwen3.5-4B on the CPU and scored 86.5% on test 2.
> - **Activation:** `apply_calibration.py` wrote the profile calibration (checkpoint 01ac12a5…) and `profiles --use` activated it.
> - **`verify_install.py --hybrid` with `TYPELOGIC_REAL_INFERENCE=1`:**
>   - 247 passed, 5 skipped;
>   - 9/9 decisions on the ONNX, Rust and PyTorch heads, with a maximum difference of 3.1×10⁻⁷;
>   - hybrid decisions ok (generalist on the CPU fallback);
>   - curated examples: 22/23 on the specialist and 23/23 on the hybrid;
>   - `passed: true`.
>
> **Cleanup (2026-10-25).** The previous profiles and runs (`decision-2b`, `decision-2b-b580-cpu-test`, `decision-2b-b580-cpu-test-v2`, `runs/qwen2b-t4-v1`, `b580-xpu-catalog-v2-v1` and smokes), the pilot datasets and leftover files in the root (about 15 GB) were removed. The tests that read `models/decision-2b` now use the active profile. After the cleanup, with `TYPELOGIC_REAL_INFERENCE=1`: 246 passed, 5 skipped; ONNX/Rust/PyTorch decisions identical (maximum difference 3.1×10⁻⁷); curated examples 22/23.

> **Windows with the real weights (2026-10-24; Ryzen 5 8600G, 15 GB RAM, Intel Arc B580).** Rust 1.99.1 + MSVC: `compilar-nativo.bat` built the core and the Python/Node/Lua/CLI extensions in ~18 s (`core.backend()` = rust). Tests without a model: 233 passed, 9 skipped; with `TYPELOGIC_REAL_INFERENCE=1`: 237 passed, 5 skipped (skipped: NATS/Redis/PostgreSQL services not configured and research tests without scipy/scikit-learn). `verify_install.py`: 9/9 real decisions with integrity verified on the ONNX, Rust and PyTorch heads (~3.2–3.6 s per decision on the CPU, ~25 s per process including loading), identical answers and a maximum probability difference of 3.3×10⁻⁷; curated examples 22/23 (error: "Security · altered checkout request", question 2). Full evaluation on the B580 (`evaluate_local.py`, fp16, 59.5 min, peak 7.4 GiB): validation 0.94494 reproduced with difference 0; test 7,583/8,260 = 91.80% (Wilson 95% 91.19–92.38%); temperature 2.1415 on the calibration split; test NLL 0.524 → 0.344, ECE 5.84% → 2.85%, Brier 0.138 → 0.132. `apply_calibration.py` wrote `models/decision-2b-b580-cpu-test-v2/calibration.json` (checkpoint de6d39a7…); a later decision showed the calibration applied. Fixes made during these tests: UTF-8 input/output in the CLI (Windows used cp1252), reading of the complete JSON and numerical tolerance in `verify_install.py`, and three tests adjusted to the embedded Windows Python.

> Release 1.0 (Rust core, TypeScript UI, Node/Lua/Elixir bindings, optional LAYA 2.0 services): see the release 1.0 notes. Linux x86-64 without backbone weights or PyTorch: Python 3.13 suite 228 passed / 4 skipped / 10 failed only on missing torch/huggingface_hub; Rust-vs-Python parity on ~180k fuzzed cases per run with zero mismatches; Chromium UI 13/13; Node SDK 5/5; Lua 57/57; Elixir 7/7; real NATS 2.11, Redis 7.0 and PostgreSQL 16; Qdrant against a REST stub only. Real Qwen3.5-2B inference, native Windows builds and XPU evaluation were not executed in this environment; run `verificar.bat` and `tools/evaluate_local.py` with the weights.

> Runtime patch v1 (training-safe): see the runtime patch v1 notes. Linux x86-64, Python 3.13 without torch/onnx: 196 passed, 4 skipped, 10 failed only on missing torch/huggingface_hub (same as the unpatched baseline). Real-weight, ONNX, Windows and XPU paths were not executed for this patch.
