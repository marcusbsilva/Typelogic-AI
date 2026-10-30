# Verification of TypeLogic 2.0.0

What was run, where, and what it showed. Real-model checks ran on one Windows 11 computer with an Intel Arc B580
(12 GB, Vulkan) and an AMD Radeon 760M; other GPUs, Windows versions and Linux were not tested.

## Automated tests (no models)

| Check | Where | Result |
|---|---|---|
| `cargo test` (core contracts, HTTP, loopback checks, routing, layout, presets, setup, MCP, Needle, metrics, ensemble) | Linux container, Windows | 42 + 13 tests pass |
| ports compared with the Python of 1.x: request format (100 cases), titles (278), evidence checks (555), Plumb prompts byte for byte (61) | Linux container | identical |
| Needle FFI against a stand-in library with the same C ABI (`tests/gen/fake_needle.c`, `cargo test -- --ignored`) | Linux container | pass |
| `web/tests/ui.test.mjs`: the interface in headless Chromium against TypeLogic.exe with a deterministic engine | Linux container | 26/26 |

## With the real models (`spike\run-spikes.bat`, B580)

| Step | What it checks | Result |
|---|---|---|
| `app` (phase 1) | JevBench, test2 and BANKING77 through TypeLogic.exe against each model measured alone; loopback only; no inference server left after a stop or a kill | specialist and Clef-Flash identical; Plumb 1 changed answer of 846 (GPU arithmetic); +15-40 ms per request |
| `ui` (phase 2) | the web interface API: template case, title by Plumb, evidence extraction, 77-option catalog, library, exports, CPU only mode and back | 14/14 |
| `phase3` | Combine against the product of experts of the separate runs; the Evaluate page (same routed accuracy as `/v1/decide`); bias probes; Needle 3 (same fields as 1.x) | 13/13 |
| `release` | the release archive extracted into an empty folder: layout, bundled runtime, no download without a console to ask, setup from local files, Needle downloaded from Hugging Face and taken out of its wheel, decision, Decision Index equal to phase 3, Needle, no llama-server left | 10/10 |

Numbers behind the README (B580, JevBench public, 231 decisions): Decision Index 83.3 (Route), 83.5 (Combine), 75.6
(Clef-Flash only); accuracy 87.9 / 87.9 / 81.8%; Brier 0.193 / 0.176 / 0.251. test2 (615 held-out questions):
Combine 88.6%, Plumb alone 85.9%. Detailed tables: `docs/SPIKE-RESULTS.md`, `spike/results/REPORT.md` (generated).

## Release archive

`tools/package.py` builds `release/TypeLogic-2.0.0-windows-x64.zip` (32 MiB, 40 files): TypeLogic.exe,
config/models.lock.json, runtime/llama (llama.cpp b11960 with the patched llama.dll), README, LICENSE,
NOTICE, licenses. `spike\run-spikes.bat release` then checks it on a clean folder.

After publishing, the four release assets were downloaded from their GitHub URLs and match `SHA256SUMS.txt` and
`config/models.lock.json` (the specialist GGUF: 2,014,112,160 bytes, SHA-256 29bc0931...).

Not verified automatically: the interactive first-start questions (they need a person at the console).
