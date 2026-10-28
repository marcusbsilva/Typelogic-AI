# typelogic-distill-v1-test2

Extra held-out test for TypeLogic AI 1.1: 213 documents and 615 questions (267 choice, 206 noul, 142 score; 145 English
and 68 Portuguese documents). Same generation, teachers and consensus rule as `typelogic-distill-v1` (see its README).

- The documents were generated after `typelogic-distill-v1` was frozen, so none of them was used for training, checkpoint
  selection or calibration.
- Documents sharing more than two 8-word sequences with any v1 document, or with the public JevBench items, were dropped
  (133 + 6).
- Questions whose labelling stopped at the spending cap before two teachers answered are left out (220).

Synthetic data is not evidence of quality in your domain. Check each provider's terms before redistributing the texts.
