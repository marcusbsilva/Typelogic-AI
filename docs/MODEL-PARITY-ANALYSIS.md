# Is it worth changing `MODEL-PARITY.json`? (2026-10-25)

## What it protects

`MODEL-PARITY.json` freezes, by SHA-256 hash, the 16 files that define how the trained specialist reads a decision:

| Group | Files |
|---|---|
| Encoder and head | `typelogic/vendor/pointer.py`, `typelogic/vendor/__init__.py` |
| Contract | `typelogic/schema.py` |
| Training and objectives | `typelogic/learning.py`, `typelogic/objectives.py`, `typelogic/training_*.py`, `typelogic/colab_support.py`, `typelogic/jev_distill.py` |
| Evaluation | `tools/evaluate_colab_v2.py` |
| Profile | `typelogic/model_profile.py`, `typelogic/offline.py` |
| Trainers | `tools/train_backbone.py`, `tools/train_colab_v2.py` |
| Configurations | the two 2B training configurations |

It also stores the hashes of the active adapter (`decision-2b-distill-s1024`) and of the training datasets. The test `tests/test_model_parity_freeze.py` fails if any of these files changes.

The reason is technical: the LoRA adapter and the decision head only work with exactly the same encoding (positions, masks, token order) they were trained with. A "harmless" change to `pointer.py` can lower accuracy without any error appearing.

## What the review found in the frozen files

| Finding | File | Actual effect | Needs a change? |
|---|---|---|---|
| Unused import (`time`, `hashlib`) | `train_backbone.py`, `train_colab_v2.py` | None | No |
| Default path `models/decision-2b/backbone` | `train_colab_v2.py` (`--base-dir`) | None in practice: the jobs always pass `--base-dir` | No |
| Backbone path hard-coded to `models/decision-2b/backbone` | `evaluate_colab_v2.py` (line 123) | Only the Colab mode of that file breaks outside Colab. Local evaluation (`evaluate_local.py`) imports only the protocol functions and works. | Only when there is another training run |
| Default profile `models/decision-2b` when there is no `models/active.json` | `model_profile.py` | None: `active.json` points to the new profile | No |

No encoding, contract or calculation bug was found in these files. The real defects found by the review were outside them: hybrid engine, local database, CLI, interface and generalist. They were fixed without touching the freeze.

## When a change would be necessary

Changing any frozen file requires a new specialist training run: about 4 to 5 hours on the B580, plus evaluation and calibration. It is only worth it together with a reason that already requires that training:

1. **Retrain with your corrected decisions.** This is the most likely reason, once there are around 100 to 200 corrected decisions.
   - The training itself does not require changing the freeze: `build_training_mix.py --feedback` and `train_colab_v2.py --init-from` already cover it.
   - It would be the time to also fix the default paths and the imports above.
2. **Train with longer states or faster.** Training currently uses states of up to 1,024 tokens because of limited VRAM.
   - This calls for changes to `pointer.py`, such as per-block checkpointing or truncating the options branch.
   - It depends on first testing `flash-linear-attention` for XPU (see `docs/OPTIONAL-COMPONENTS.md`), which may solve it without changing the code.
3. **Change the base model** (for example, to a newer Qwen). This changes `model_profile.py` and `pointer.py` entirely and starts an adapter from scratch.

## Known limits outside the freeze

- **Specialist calibration:** the fingerprint (`engine.checkpoint_fingerprint`) covers the weights and the configuration, but not the precision or the readout head (`onnx`, `rust`, `torch`).
  - The temperature was fitted on the B580 in fp16 and is also applied on the CPU in bf16. In the tests, bf16 and fp32 gave the same answer in 20 of 20 cases, and the three heads differ by less than 3.1×10⁻⁷. The expected impact is small, but it was not measured for calibration.
  - Including the precision in the fingerprint would invalidate the current calibration, so it was left for the next training run.
- **Generalist calibration (fixed in this release):** the file now only applies if the weights (PyTorch) and the readout form (one or two orders) are the same as when it was fitted. For Ollama, the model is identified by the tag in the file name.

## Conclusion

**There is currently no need to change `MODEL-PARITY.json`.** The findings in the frozen files are cosmetic or only affect the Colab mode, and changing any of them would require retraining and re-evaluating with no quality gain. The recommendation is to make these small fixes together with the next retraining, when there is enough reviewed data, and to update the manifest at that point with the new adapter.
