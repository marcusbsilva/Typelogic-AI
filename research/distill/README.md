# Dataset generation pipeline (typelogic-distill-v1)

Scripts that generated `research/data/typelogic-distill-v1` and `typelogic-distill-v1-test2` through OpenRouter. They are research tools: the application never imports them, and they run only when you start them.

**Key:** set `OPENROUTER_API_KEY` in your shell. The scripts never print or write it. Every call is logged with its cost in `work/ledger.jsonl`, and each step stops when the ledger reaches the budget you pass. Work files go to `research/distill/work/` (ignored by Git) or to `TYPELOGIC_DISTILL_WORK`.

| Step | Script | What it does |
|---|---|---|
| 1 | `generate.py BUDGET COUNT` | An author model writes a realistic fictional document (policy + case facts, email thread, ticket, log or form) in English or Portuguese across 43 domains and 10 reasoning skills, with 3–5 typed questions and its own answers. Default authors: `deepseek/deepseek-v4.1-flash`, `qwen/qwen3.8-flash`. |
| 2 | `label.py BUDGET [TEACHERS]` | Teachers read each question with the generalist's direct option-letter prompt. Teachers that return log-probabilities give soft distributions; the others give one letter. Default: `qwen/qwen3.6-35b-a3b`, `qwen/qwen3.8-flash`, `deepseek/deepseek-v4.1-flash`, `google/gemma-4-26b-a4b-it`. |
| 2b | `label_router.py BUDGET COUNT` | Optional: a fixed sample read by `typesafe/jev-router` (records the routed model). |
| 3 | `assemble.py [test2]` | Consensus (author + teachers; kept when the soft consensus equals the author answer and at least 3 voters agree), soft targets, decontamination against JevBench, splits by document, manifest with SHA-256. `test2` builds the extra test from documents generated after v1 was frozen. |

Cost of the published datasets: about US$ 3.80 in total. Model names are those available on OpenRouter in October 2026; replace them with current ones. Check each provider's terms before redistributing generated texts.
