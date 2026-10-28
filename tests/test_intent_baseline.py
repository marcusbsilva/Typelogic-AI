import importlib.util
import math
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from typelogic.benchmark_metrics import metrics, align_probabilities, paired_comparison
spec=importlib.util.spec_from_file_location('baseline',ROOT/'tools/benchmark_intents.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

class MetricsTests(unittest.TestCase):
 def test_ids_align_not_dictionary_order(self):
  self.assertEqual(align_probabilities({'B':.8,'A':.2},['A','B']),[.2,.8])
 def test_missing_extra_and_nan_rejected(self):
  for p in [{'A':1.},{'A':.5,'B':.4,'C':.1},{'A':float('nan'),'B':.5},{'A':.8,'B':.8}]:
   with self.assertRaises(ValueError):align_probabilities(p,['A','B'])
 def test_hand_computed_metrics(self):
  r=metrics(['A','B'],['A','B'],[[.8,.2],[.4,.6]])
  self.assertEqual(r['correct'],2);self.assertAlmostEqual(r['macro_f1'],1)
  self.assertAlmostEqual(r['nll'],-(math.log(.8)+math.log(.6))/2)
  self.assertAlmostEqual(r['brier'],.2)
 def test_no_accepted_cases_is_null_not_perfect(self):
  r=metrics(['A','B'],['A','B'],[[.6,.4],[.4,.6]])
  self.assertIsNone(r['selective']['0.95']['accuracy'])
  self.assertEqual(r['selective']['0.95']['coverage'],0)
 def test_unknown_gold_rejected(self):
  with self.assertRaises(ValueError):metrics(['A','B'],['C'],[[.5,.5]])
 def test_macro_f1_counts_missing_class(self):
  self.assertEqual(metrics(['A','B'],['A'],[[1.,0.]])['macro_f1'],.5)
 def test_paired_requires_identical_case_identity(self):
  a=[{'id':'1','state_sha256':'a','question_sha256':'q','gold':'A','probabilities':{'A':.9,'B':.1}}]
  c=[{'id':'1','state_sha256':'b','question_sha256':'q','gold':'A','probabilities':{'A':.9,'B':.1}}]
  with self.assertRaises(ValueError):paired_comparison(a,c)
 def test_paired_requires_exact_ids(self):
  a=[{'id':'1','state_sha256':'a','question_sha256':'q','gold':'A','probabilities':{'A':.9,'B':.1}}]
  with self.assertRaises(ValueError):paired_comparison(a,[])
 def test_paired_rejects_changed_question(self):
  a=[{'id':'1','state_sha256':'a','question_sha256':'q','gold':'A','probabilities':{'A':.9,'B':.1}}]
  with self.assertRaises(ValueError):paired_comparison(a,[{**a[0],'question_sha256':'different'}])
 def test_paired_disagreements(self):
  a=[{'id':'1','state_sha256':'a','question_sha256':'q','gold':'A','probabilities':{'A':.9,'B':.1}}]
  c=[{**a[0],'probabilities':{'B':.9,'A':.1}}]
  r=paired_comparison(a,c)
  self.assertEqual(r['left_only_correct'],1);self.assertEqual(r['accuracy_delta_left_minus_right'],1)

class BaselineTests(unittest.TestCase):
 def test_calibration_temperature_keeps_argmax(self):
  import numpy as np
  z=np.array([[1.,2.,0.]])
  self.assertEqual(int(b.probabilities(z,.4).argmax()),1)
 @unittest.skipUnless(importlib.util.find_spec('scipy'),'research baseline needs scipy (research/requirements-baseline.txt)')
 def test_temperature_fitted_on_calibration_nll(self):
  import numpy as np
  z=np.array([[8.,0.],[8.,0.],[8.,0.]])
  t=b.fit_temperature(z,['A','B','B'],['A','B'])
  self.assertGreater(t,1.)
 @unittest.skipUnless(importlib.util.find_spec('sklearn'),'research baseline needs scikit-learn (research/requirements-baseline.txt)')
 def test_features_fit_train_only(self):
  f=b.vectorizer();f.fit_transform(['bank card payment','cancel bank card','payment card bank'])
  before=[dict(v.vocabulary_) for _,v in f.transformer_list]
  f.transform(['uniquetestword'])
  self.assertEqual(before,[v.vocabulary_ for _,v in f.transformer_list])
  self.assertNotIn('uniquetestword',f.transformer_list[0][1].vocabulary_)
 def test_unknown_source_fails(self):
  with self.assertRaises(ValueError):b.select_source({'train':[],'validation':[],'calibration':[],'test':[]},'missing')

if __name__=='__main__':unittest.main()
