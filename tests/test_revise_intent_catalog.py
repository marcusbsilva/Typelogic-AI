import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
from revise_intent_catalog import revise
from typelogic.learning import file_sha256


class CatalogTests(unittest.TestCase):
    def fixture(self,root):
        source=root/'source';source.mkdir()
        def record(rid,label,split):
            return {'id':rid,'source_id':'banking77','split':split,'state':'Query '+rid,
                'group_id':rid,'language':'en','gold_label':label,
                'questions':{'intent':{'type':'choice','instructions':'Choose.',
                    'criteria':{'a':'Old A','b':'Old B'}}},
                'targets':{'intent':{'a':float(label=='a'),'b':float(label=='b')}},'teacher_used':False}
        rows={'train':[record('t0','a','train'),record('t1','a','train'),record('t2','b','train')],
              'validation':[record('v','a','validation')],'calibration':[record('c','b','calibration')],
              'test':[record('x','a','test')]}
        for split,items in rows.items():
            (source/(split+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in items),encoding='utf-8')
        files={s+'.jsonl':file_sha256(source/(s+'.jsonl')) for s in rows}
        (source/'manifest.json').write_text(json.dumps({'files':files}),encoding='utf-8')
        catalog={'source_id':'banking77','language':'en','revision':'test-v2',
            'source_train_sha256':files['train.jsonl'],'descriptions':{'a':'New A','b':'New B'},
            'evidence_train_ids':{'a':['t1'],'b':['t2']}}
        policy={'decisions':[{'id':'t0','action':'quarantine','reason':'No context'}]}
        return source,rows,catalog,policy

    def test_versioned_output_preserves_labels_states_groups_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source,rows,catalog,policy=self.fixture(root)
            original=(source/'train.jsonl').read_bytes()
            report=revise(source,catalog,policy,root/'out')
            self.assertEqual(report['counts']['train'],2)
            self.assertEqual(report['quarantined'],1)
            for split in rows:
                actual=[json.loads(s) for s in (root/'out'/(split+'.jsonl')).read_text().splitlines()]
                expected=[r for r in rows[split] if r['id']!='t0']
                for a,e in zip(actual,expected):
                    for key in ('id','state','group_id','gold_label','targets','split'):
                        self.assertEqual(a[key],e[key])
                    self.assertEqual(list(a['questions']['intent']['criteria']),['a','b'])
                    self.assertEqual(a['questions']['intent']['criteria']['a'],'New A')
                    self.assertFalse(a['teacher_used'])
            self.assertEqual((source/'train.jsonl').read_bytes(),original)
            with self.assertRaises(ValueError):revise(source,catalog,policy,root/'out')

    def test_heldout_cannot_be_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            source,_,c,p=self.fixture(Path(d));c['evidence_train_ids']['a']=['v']
            with self.assertRaises(ValueError):revise(source,c,p,Path(d)/'out')

    def test_rejects_changed_input_and_mismatched_catalog_ids(self):
        with tempfile.TemporaryDirectory() as d:
            source,_,c,p=self.fixture(Path(d));c['descriptions']={'a':'A','other':'B'}
            with self.assertRaises(ValueError):revise(source,c,p,Path(d)/'out')
            (source/'test.jsonl').write_text('{}\n')
            with self.assertRaises(ValueError):revise(source,c,p,Path(d)/'out2')

    def test_rejects_distilled_input_and_missing_quarantine_id(self):
        with tempfile.TemporaryDirectory() as d:
            source,_,c,p=self.fixture(Path(d));p['decisions'][0]['id']='absent'
            with self.assertRaises(ValueError):revise(source,c,p,Path(d)/'out')

    def test_rejects_old_soft_targets_even_with_updated_file_hash(self):
        with tempfile.TemporaryDirectory() as d:
            source,rows,c,p=self.fixture(Path(d))
            rows['train'][1]['targets']['intent']={'a':.9,'b':.1}
            path=source/'train.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows['train']))
            m=json.loads((source/'manifest.json').read_text())
            m['files']['train.jsonl']=file_sha256(path)
            (source/'manifest.json').write_text(json.dumps(m))
            c['source_train_sha256']=file_sha256(path)
            with self.assertRaises(ValueError):revise(source,c,p,Path(d)/'out')


if __name__=='__main__':unittest.main()
