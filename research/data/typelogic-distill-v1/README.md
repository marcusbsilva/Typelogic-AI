# typelogic-distill-v1

Synthetic typed decisions (noul, choice, score) over long policy and case documents, in English and Portuguese,
built for TypeLogic AI 1.1 on 24/10/2026.

- **Generation:** an OpenRouter model writes a document (policy excerpt + case facts) and 3–5 questions with the
  author's answer (`provenance.author`).
- **Labels:** four open-weight teachers (Qwen3.6-35B-A3B, Qwen3.8-Flash, DeepSeek-V4.1-Flash, Gemma-4-26B-A4B) read each
  question with the same direct option-letter prompt as the generalist (`semif-direct-options-v1`). The two Qwen teachers
  return log-probabilities (soft distributions); the others return one letter. A sample of 79 questions was also read by
  `typesafe/jev-router` (the Jev router; `provenance.teachers` records the model it routed to): it agrees with the kept
  consensus on 63 of 66 questions.
- **Consensus:** the target is the soft average of the author answer and the teacher distributions; a question is kept
  only when that average picks the author answer and at least 3 voters agree (2,985 of 3,456 questions kept).
- **Decontamination:** documents sharing more than two 8-word sequences with the public JevBench items were dropped (8).
- **Splits:** by document (85/5/5/5); `group_id` = document id. `manifest.json` has counts and SHA-256 per split.

Synthetic data is not evidence of quality in your domain. The texts were generated and labelled with third-party models:
check each provider's terms before redistributing them.
