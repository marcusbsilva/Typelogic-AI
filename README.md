# TypeLogic AI 2.0

TypeLogic answers typed decisions about a case (Yes / no, Choice, Score), with a probability for every option,
offline on your own computer. You describe the case, keep the facts that matter as typed evidence and ask your
questions; TypeLogic answers, records which model answered, flags what needs review and keeps every decision with a
checksum in a local library.

Version 2 is one executable. **TypeLogic.exe** (Rust standard library, no third-party crates) is the launcher, the
web interface, the API and the supervisor of **llama.cpp**, which runs the models on the GPU (Vulkan) or the CPU.
There is no Python, Ollama or PyTorch to install; nothing listens outside `127.0.0.1`, and nothing is downloaded
while TypeLogic runs.

## Download and first start

Requirements: Windows 10 or 11 (x64), about 5 GB of disk (10 GB with Clef-Flash). A GPU with Vulkan support is used
when there is one (tested on an Intel Arc B580); without one the models run on the CPU.

1. Download `TypeLogic-2.0.0-windows-x64.zip` from the [releases](https://github.com/marcusbsilva/TypeLogic-AI/releases)
   and extract it to a folder you can write to (not `Program Files`).
2. Double-click `TypeLogic.exe`. On the first start it lists the models it needs, asks before downloading
   (about 4.4 GB; Clef-Flash, 5 GB more, is offered when the GPU has room) and checks every file against the
   SHA-256 in `config/models.lock.json`.
3. The web interface opens at `http://127.0.0.1:8765`. Close the console window (or press Ctrl+C) to stop
   TypeLogic and the models.

From a terminal:

```bat
TypeLogic                         rem the web interface and the models (default command)
TypeLogic setup [--with-clef]     rem install or repair the runtime and the models; --from DIR uses local files
TypeLogic doctor                  rem files, GPUs and the models this computer will load
TypeLogic import typelogic-export.json   rem add the library of TypeLogic 1.x (Library > Export all)
TypeLogic mcp                     rem Model Context Protocol server on stdio (tool: decide)
TypeLogic help
```

## Models

Each model has one role, chosen by measurement (see [docs/SPIKE-RESULTS.md](docs/SPIKE-RESULTS.md)):

| Role | Model | On disk | Used for |
|---|---|---|---|
| generalist | Plumb-4B v5 Q4_K_M (Apache-2.0) | 2.5 GB | questions with up to 16 options (direct option-letter readout), titles, evidence extraction |
| specialist | TypeLogic Qwen3.5-2B + LoRA + decision head, Q8_0 (Apache-2.0) | 1.9 GB | questions with more than 16 options when Clef-Flash is not loaded |
| catalogs (optional) | Clef-Flash IQ4_XS (Apache-2.0) | 5.0 GB | questions with more than 16 options; the most accurate on catalogs |
| extractor | Needle 3 (Apache-2.0) | 36 MB | typed fields from short texts |

At every start TypeLogic loads what fits this computer: Plumb + Clef-Flash on a GPU with about 9 GB free,
specialist + Plumb on a smaller GPU, both on the CPU otherwise (or when you choose CPU only). `TypeLogic doctor`
shows the choice before anything starts.

## Using it

- **New decision:** a case text with an optional title and tags, typed evidence (template values, your own, or
  extracted by Plumb or Needle and kept only after you confirm them) and Yes / no, Choice or Score decisions. Each
  answer shows the probabilities, the confidence (top probability, a model estimate, not accuracy), the model that
  answered and the review reasons. Choice decisions can be read again with the options reordered to show how much
  the order matters.
- **Templates:** 14 built-in ones; your own are saved in `data/templates`.
- **Library:** every decision with its checksum (SHA-512, MD5 for compatibility), tags, corrections, Lite and full
  exports. It keeps up to 200 decisions; then, after you confirm, all of them are compressed into
  `data/archives/DD-MM-YYYY.rar` (WinRAR) or `.zip`, tested, and only then removed.
- **Evaluate:** the 231 public JevBench decisions (MIT, built in) or your own JSONL file with gold answers, run
  through the loaded models. It shows the **Decision Index** (0-100: chance-corrected accuracy per tier, clipped at
  0, with JevBench's tier weights; not the JevBench leaderboard score), accuracy with failures counted wrong, Brier,
  ECE and NLL per tier, question type and model, selective accuracy, and four bias probes: option order, first /
  last option against the gold answers, Yes polarity and Score leniency. Runs are saved in `data/evaluations`;
  nothing goes to the library.
- **Engine & privacy:** Automatic or CPU only, and how questions with up to 16 options are answered: **Route**
  (Plumb alone, fastest) or **Combine** (Plumb and the decision model together, a weighted product of their
  probabilities: better calibrated, about a third slower).

Everything you create stays in the `data` folder next to TypeLogic.exe. Stop TypeLogic before copying it as a backup.

Measured on the B580 (JevBench public, 231 decisions): Decision Index 83.3 with Route and 83.5 with Combine
(accuracy 87.9%, Brier 0.193 and 0.176); on 615 held-out questions of TypeLogic's own test set, Combine reaches
88.6% against 85.9% for Plumb alone. These are local measurements, not guarantees for your cases: use Evaluate with
your own questions.

## API (127.0.0.1 only)

| Request | What it does |
|---|---|
| `POST /v1/decide` | a decision: `{"state", "questions", "threshold"?, "permutation_check"?}` (the 1.x contract); every answer records its `engine`; not stored |
| `POST /v1/decide?engine=generalist\|specialist\|clef` | the same with one model forced |
| `POST /v1/decide?combine=ensemble` | questions with up to 16 options read by Plumb and by Clef-Flash (or the specialist), `p ~ plumb^w * other^(1-w)` (w = 0.5 with Clef-Flash, 0.55 with the specialist); answers carry `engine: ensemble` and both components |
| `POST /v1/systemone?engine=specialist\|clef` | the System One API of a decision model, unchanged |
| `GET /api/health` | version, loaded models and why they were chosen |
| `/api/*` | the API of the web interface (needs the session token of the page) |

The machine API accepts requests from this computer only (loopback address, Host and Origin checked).

MCP client configuration:

```json
{"mcpServers": {"typelogic": {"command": "C:\\TypeLogic\\TypeLogic.exe", "args": ["mcp"]}}}
```

## Limits

- The models are not calculators, authorization mechanisms or sources of verified facts. A valid answer format
  does not mean a correct answer; review what the answers flag.
- A Score question has at most 10 levels (the limit of llama.cpp's System One API).
- The specialist was trained with states of up to 1,024 tokens; longer states are answered but not validated.
- Needle reads short texts only (1,500 characters for evidence) and misses values; its values stay unverified.
- Checksums detect changes against a stored baseline; they are not signatures.
- Windows only for now; the code builds on Linux, but no Linux runtime is pinned yet.

## Building from source

Rust 1.83 or later (MSVC toolchain on Windows), Node.js only to rebuild the interface.

```bat
cargo build --release              rem target\release\TypeLogic.exe
cargo test                         rem unit tests (core contracts, HTTP, routing, layout, setup, MCP, Needle, metrics)
cd web && npm run build            rem TypeScript check and bundle into web\static\app.js (embedded in the exe)
cargo build --features fixture && node web\tests\ui.test.mjs   rem interface test with a deterministic engine
python tools\package.py            rem the release archive and assets in release\ (needs dist\ from spike\run-spikes.bat app)
```

`spike\run-spikes.bat` (development only, uses the Python of TypeLogic 1.x) converts the specialist, measures every
model and checks the application with the real models: `app` (phase 1), `ui` (phase 2), `phase3`, `release`.
Training, calibration and dataset tools stay in TypeLogic 1.x ([branch v1](https://github.com/marcusbsilva/TypeLogic-AI/tree/v1)).

```
crates/typelogic-core      contracts shared with 1.x: validation, canonical JSON, digests, exports
crates/typelogic-app       TypeLogic.exe: launcher, web UI/API, library, routing, Plumb readout, evaluation,
                           Needle, setup, MCP, llama-server supervisor
web/                       the web interface (TypeScript, built into web/static and embedded in the exe)
templates/builtin/         decision templates (embedded)
bench/jevbench-public/     the 231 public JevBench items of Evaluate (MIT, embedded)
config/models.lock.json    pinned sources (revision, size, SHA-256) of the runtime and the models
tools/package.py           builds the release archive
spike/                     measurements behind the choices (development only)
docs/                      plan, measurements, verification
```

## License

Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). llama.cpp is MIT-licensed; each model keeps its own
license (listed in `config/models.lock.json`). TypeLogic 1.x remains available in the `v1` branch and the `v1.1.0`
release.
