import importlib.util
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('runner',ROOT/'tools/evaluate_bilingual.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class RegressionTests(unittest.TestCase):
    def test_boolean_is_not_truthiness(self):
        self.assertEqual(m.prediction({'probabilities':{'false':.9,'true':.1}}),'false')
    def test_score_is_argmax_not_rounded_mean(self):
        self.assertEqual(m.prediction({'score':.8,'probabilities':{'0':.6,'1':0.,'2':.4}}),'0')
    def test_metrics_separate_language_and_type(self):
        rows=[{'language':'en','type':'noul','correct':True},{'language':'pt-BR','type':'noul','correct':False}]
        s=m.summarize(rows)
        self.assertEqual(s['en/noul']['accuracy'],1.)
        self.assertEqual(s['pt-BR/noul']['accuracy'],0.)
    def test_fixture_contract(self):
        rows=m.read_cases(ROOT/'research/data/regression/bilingual-v1.jsonl')
        self.assertEqual(len(rows),18)
        self.assertEqual({r['language'] for r in rows},{'en','pt-BR'})
        from collections import Counter
        self.assertEqual(set(Counter(r['group_id'] for r in rows).values()),{2})
if __name__=='__main__':unittest.main()
