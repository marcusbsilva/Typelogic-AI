import importlib.util
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('evaluate_colab_v2',ROOT/'tools/evaluate_colab_v2.py')
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EvaluationIdentityTests(unittest.TestCase):
    def test_matching_checkpoint_dataset_keeps_reproduction_guard(self):
        hashes={'train':'a','validation':'b'}
        self.assertTrue(MODULE.check_dataset_identity(hashes,dict(hashes)))
        result=MODULE.validation_reproduction(.8,.8,True)
        self.assertTrue(result['performed'])
        self.assertEqual(result['absolute_difference'],0)

    def test_dataset_mismatch_is_rejected_by_default(self):
        with self.assertRaisesRegex(ValueError,'Dataset hashes differ'):
            MODULE.check_dataset_identity({'train':'new'},{'train':'old'})

    def test_explicit_mismatch_skips_only_reproduction(self):
        self.assertFalse(MODULE.check_dataset_identity({'train':'new'},{'train':'old'},True))
        result=MODULE.validation_reproduction(.8,.7,False)
        self.assertFalse(result['performed'])
        self.assertEqual(result['loaded_accuracy_on_evaluation_dataset'],.7)
        self.assertNotIn('absolute_difference',result)


if __name__=='__main__':
    unittest.main()
