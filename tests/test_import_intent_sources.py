import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('importer', Path(__file__).resolve().parents[1] / 'tools/import_intent_sources.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ImportTests(unittest.TestCase):
    def source(self, sid="banking77"):
        return {'revision':'a'*40,'license':m.ALLOWED[sid],'language':'en','origin_kind':'natural_queries'}

    def row(self, text='My card is missing', label='lost', split='train', index=0, source='banking77'):
        return m.make_record(source, self.source(source), split, index, text, label)

    def test_preserves_text_gold_and_source_split(self):
        r=self.row('  My CARD is missing!  ', 'lost', 'test', 2)
        self.assertEqual(r['state'], '  My CARD is missing!  ')
        self.assertEqual(r['gold_label'], 'lost')
        self.assertEqual(r['source_split'], 'test')
        self.assertEqual(r['source_record_id'], 'test:2')
        self.assertEqual(r['language'], 'en')

    def test_normalization_keeps_negation_numbers_punctuation(self):
        self.assertEqual(m.normalize(' MY\tcard '), m.normalize('my card'))
        self.assertNotEqual(m.normalize('has arrived'),m.normalize('has not arrived'))
        self.assertNotEqual(m.normalize('pay 30'),m.normalize('pay 300'))
        self.assertNotEqual(m.normalize('a-b'),m.normalize('ab'))

    def test_cross_split_duplicates_quarantined_without_moving_rows(self):
        rows=[self.row(),self.row(' MY card is missing ',split='test')]
        out, report=m.audit(rows, {})
        self.assertEqual([r['source_split'] for r in out],['train','test'])
        self.assertTrue(all('cross_split_exact' in r['exclusion_reasons'] for r in out))
        self.assertEqual(report['cross_split_exact_groups'],1)

    def test_conflicting_gold_quarantined_not_relabelled(self):
        rows=[self.row(),self.row(label='arrival',index=1)]
        out,_=m.audit(rows,{})
        self.assertEqual([r['gold_label'] for r in out],['lost','arrival'])
        self.assertTrue(all('conflicting_gold' in r['exclusion_reasons'] for r in out))

    def test_independent_ontologies_are_not_label_conflicts(self):
        out,report=m.audit([self.row(),self.row(source='clinc150',label='report_lost_card')],{})
        self.assertEqual(report['conflicting_gold_groups'],0)
        self.assertEqual(len({r['group_id'] for r in out}),1)

    def test_prior_heldout_exposure_blocks_training(self):
        out,_=m.audit([self.row()],{m.normalize('My card is missing'):[{'file':'old-validation.jsonl','role':'heldout'}]})
        self.assertIn('prior_heldout',out[0]['exclusion_reasons'])
        self.assertFalse(out[0]['training_candidate'])

    def test_prior_training_is_recorded(self):
        out,_=m.audit([self.row()],{m.normalize('My card is missing'):[{'file':'old-train.jsonl','role':'train'}]})
        self.assertTrue(out[0]['prior_exposure'])
        self.assertTrue(out[0]['training_candidate'])
        self.assertFalse(out[0]['fresh_evaluation_eligible'])

    def test_audit_does_not_mutate_input(self):
        rows=[self.row()]; original=copy.deepcopy(rows)
        m.audit(rows,{})
        self.assertEqual(rows,original)

    def test_unknown_clinc_label_rejected(self):
        data={'train':[['hello','greet']], 'val':[['hello','invented']], 'test':[], 'oos_train':[], 'oos_val':[], 'oos_test':[]}
        with self.assertRaises(ValueError): m.parse_clinc(data,self.source("clinc150"))

    def test_oos_preserved_as_oos(self):
        data={'train':[['hello','greet']], 'val':[], 'test':[], 'oos_train':[['unrelated','oos']], 'oos_val':[], 'oos_test':[]}
        rows=m.parse_clinc(data,self.source("clinc150"))
        self.assertEqual(rows[1]['gold_label'],'oos')
        self.assertEqual(rows[1]['source_split'],'oos_train')
        self.assertTrue(rows[1]['is_oos'])

    def test_malformed_clinc_not_silently_skipped(self):
        data={'train':[['hello','greet']], 'val':[], 'test':[], 'oos_train':[['broken']], 'oos_val':[], 'oos_test':[]}
        with self.assertRaises(ValueError): m.parse_clinc(data,self.source("clinc150"))

    def test_bad_license_and_empty_text_rejected(self):
        src=self.source();src['license']='unknown'
        with self.assertRaises(ValueError): m.make_record('banking77',src,'train',0,'x','a')
        with self.assertRaises(ValueError): self.row(' ')

    def test_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'file';p.write_text('changed')
            with self.assertRaises(ValueError): m.verify_file(p,{'sha256':'0'*64,'bytes':7})

    def test_same_split_duplicate_has_single_candidate(self):
        out,_=m.audit([self.row(),self.row(index=1)],{})
        self.assertEqual(sum(r['training_candidate'] for r in out),1)

    def test_near_match_flagged_for_review(self):
        a=self.row('please help me find the missing bank card today')
        b=self.row('please help me find the missing bank card today!',split='test')
        out,report=m.audit([a,b],{})
        self.assertEqual(report['near_duplicate_review_pairs'],1)
        self.assertTrue(all('near_duplicate_review' in r['exclusion_reasons'] for r in out))

if __name__=='__main__': unittest.main()
