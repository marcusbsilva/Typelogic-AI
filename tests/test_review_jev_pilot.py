import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from collect_jev_intents import digest
from typelogic.learning import file_sha256
from review_jev_pilot import review_dataset


class PilotReviewTests(unittest.TestCase):
    def test_review_preserves_input_and_applies_three_actions(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);train=root/'train.jsonl';teachers=root/'teachers';teachers.mkdir()
            rows=[{'id':str(i),'split':'train','source_id':'banking77','state':'State '+str(i),
                   'group_id':str(i),'gold_label':'a',
                   'questions':{'intent':{'type':'choice','instructions':'Choose.',
                       'criteria':{'a':'A','b':'B'}}},'targets':{'intent':{'a':1.,'b':0.}}}
                  for i in range(4)]
            train.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
            original=train.read_bytes()
            identity={'model':'typesafe/jev-1.13','train_sha256':file_sha256(train),
                'ids':['0','1','2'],'row_sha256':[digest(r) for r in rows[:3]]}
            (teachers/'identity.json').write_text(json.dumps(identity),encoding='utf-8')
            for r in rows[:3]:
                (teachers/(digest(r)+'.json')).write_text(json.dumps({'id':r['id'],
                    'split':'train','source_id':r['source_id'],'row_sha256':digest(r),
                    'model':'typesafe/jev-1.13-20260917',
                    'probabilities':{'a':.8,'b':.2}}),encoding='utf-8')
            policy={'teacher_weight':.3,'decisions':[
                {'id':'0','action':'blend','reason':'Clear'},
                {'id':'1','action':'gold_only','reason':'Review'},
                {'id':'2','action':'quarantine','reason':'Unsupported'}]}
            report=review_dataset(train,teachers,policy,root/'audited')
            saved_report=json.loads((root/'audited/report.json').read_text(encoding='utf-8'))
            actual=[json.loads(s) for s in (root/'audited/train.jsonl').read_text().splitlines()]
            self.assertEqual(report['counts']['blend'],1)
            self.assertEqual(report['counts']['gold_only'],1)
            self.assertEqual(report['counts']['quarantine'],1)
            self.assertIn('3 English training cases',saved_report['review_scope'])
            self.assertEqual([r['id'] for r in actual],['0','1','3'])
            self.assertAlmostEqual(actual[0]['targets']['intent']['a'],.94)
            self.assertEqual(actual[1]['targets'],rows[1]['targets'])
            self.assertEqual(train.read_bytes(),original)
            self.assertTrue((root/'audited/quarantine.jsonl').is_file())
            with self.assertRaises(ValueError):
                review_dataset(train,teachers,policy,root/'audited')
            with self.assertRaises(ValueError):
                review_dataset(train,teachers,{'teacher_weight':.3,'decisions':policy['decisions'][:1]},root/'incomplete')


if __name__=='__main__':
    unittest.main()
