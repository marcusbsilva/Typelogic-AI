# Optional components: what contributes to performance (2026-10-25, updated 2026-10-26)

Summary: the application did not need the NATS, Redis, PostgreSQL and Qdrant connectors or the Elixir gateway, so they were **removed in release 1.1** (2026-10-26), together with `tools/laya_stack.py` and `TypeLogic.bat stack`. Local memory (references and document import) and the in-process recent list were removed on 2026-10-26 because they were not used. The assessment that led to the removals is kept below.

Assessment of the optional components of release 1.0 (LAYA 2.0 stack and bindings) against TypeLogic 1.1: Qwen3.5-2B
specialist on the Intel Arc B580, Plumb-4B generalist through Ollama, on a single PC with 16 GB of RAM.

## Where time and memory are spent

Almost all the time of a decision is spent on model readout:

| Stage | Measured time |
|---|---|
| Plumb-4B through Ollama | about 1 s per long document |
| fp16 specialist on the B580 | about 0.3 s per document |
| Specialist on the CPU | several seconds |
| Contracts and integrity (Rust core) | milliseconds |

The hardware limit is memory: 12 GB of VRAM and 16 GB of RAM. Lack of memory is what stalled training with 1,536-token states and what overflowed the VRAM with the specialist and Plumb loaded together. A component only improves performance if it speeds up model readout or frees memory.

## Components assessed

| Component | Purpose | Does it contribute to performance? | Recommendation |
|---|---|---|---|
| **Rust core** (`typelogic._core`) | Contracts, integrity, router, decision head | Yes. Already in use; keeps the deterministic parts in milliseconds and is identical to Python. | **Keep** |
| **In-process short-term memory** ("recent" list) | Summary of the latest decisions of each workspace (`GET /api/recent`) | Lightweight, but nothing in the app used it. | **Removed** (2026-10-26, with local memory) |
| **NATS** (message bus) | Integrate other processes through `decision.request` | No. Adds a local network hop and another process. It also accepts decisions from any local client without the session token. | **Removed** |
| **Redis** (external short-term memory) | The same recent list, outside the process | No. The in-process equivalent already does the same, with no extra RAM cost. | **Removed** |
| **PostgreSQL** (permanent replica) | Audited copy of the SQLite database | No. SQLite remains the source of truth; the replica costs memory and a process. The audit found interleaved transactions on the same connection. | **Removed** |
| **Qdrant** (vectors) | Vector search over references | Not at this scale. The vectors are local (hashed) features, not semantic embeddings; in-process search takes milliseconds. A slow Qdrant query blocks the whole database. | **Removed** |
| **Elixir/OTP gateway** (`control/`) | Agents and sessions over HTTP | No. Requires the Elixir toolchain and is not used by the application. | **Removed** |
| **`tools/laya_stack.py`** and `TypeLogic.bat stack` | Start and stop the services above | Exists only for them. | **Removed** |
| **TypeScript SDK** (`sdk/typescript`) | Call the core from another Node.js app | Does not change TypeLogic performance and costs nothing at runtime. | Keep, if integrating with other apps |
| **Lua module** (`sdk/lua`) | Same core in Lua (games, scripts) | Same as the TypeScript SDK. | Keep, if using it from Lua |

**Audit findings in the removed stack (they applied only when the external services were configured):**
- reconciliation with SQLite can recreate in PostgreSQL a decision deleted during reconciliation;
- transactions from different threads get mixed on the same PostgreSQL connection;
- after a timeout, Redis and PostgreSQL responses can be out of sync;
- Qdrant is queried with the database locked.

Fixing these would cost more than the services are worth on a single-user PC, so the external connectors were removed. The `typelogic-cli` JSON-lines bridge in `rust/`, which the Elixir gateway used, stays as a generic way to call the core from any language.

## What can actually speed up TypeLogic

None of these items was measured on the test machine. They are candidates, in order of expected impact:

1. **`flash-linear-attention` with Intel support (`fla[xpu]`).**
   - What changes: replaces the reference implementation of Gated DeltaNet (the "falling back" warning in the logs) with optimized kernels. Affects training (currently about 15 s per example) and the specialist on the GPU.
   - Requirement: Triton for XPU on Windows.
   - How to test: in a separate environment, without touching the one used for training.
2. **llama.cpp with SYCL for the generalist.**
   - What changes: on Battlemage GPUs, prefill (which is exactly the cost of direct readout) has been reported up to 5 times faster than on Vulkan.
   - Cost: one more server, in place of Ollama.
3. **Ollama settings.**
   - `OLLAMA_FLASH_ATTENTION=1` and a `q8_0` KV cache (`OLLAMA_KV_CACHE_TYPE`) reduce Plumb's VRAM and leave more room for the specialist on the GPU.
   - `OLLAMA_NUM_PARALLEL=1` avoids reserving memory for parallel requests that TypeLogic does not make.
4. **Optional 4B weights (done).** The generalist on the CPU is only attempted when the Qwen3.5-4B weights are in `models\qwen35-4b` (`TypeLogic.bat models --only weights-4b`); without them, Ollama is the only generalist.
