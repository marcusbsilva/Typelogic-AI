import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('importer',ROOT/'tools/import_intent_sources.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class IntegrationTests(unittest.TestCase):
    def test_full_corpus_reproducibility_integrity_and_non_overwrite(self):
        expected=json.loads((ROOT/'research/data/intent-import-v1/report.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'imported'
            args=(ROOT/'research/data/raw',out,ROOT/'research/sources/intent-sources.lock.json',
                  [ROOT/'research/exposure-snapshot'/n for n in ('prepared','bilingual-v2','regression')])
            report=m.run(*args)
            self.assertEqual(report['output_sha256'],expected['output_sha256'])
            self.assertEqual(report['records'],36783)
            rows=[json.loads(line) for line in (out/'records.jsonl').read_text().splitlines()]
            self.assertEqual(len({r['id'] for r in rows}),len(rows))
            candidates=[r for r in rows if r['training_candidate']]
            heldout={r['group_id'] for r in rows if m.partition(r['source_split'])!='train'}
            self.assertFalse({r['group_id'] for r in candidates} & heldout)
            self.assertFalse(any(r['exclusion_reasons'] for r in candidates))
            self.assertEqual(sum(r['is_oos'] for r in rows),1200)
            with self.assertRaisesRegex(ValueError,'already exists'):m.run(*args)
            self.assertEqual(m.digest((out/'records.jsonl').read_bytes()),expected['output_sha256']['records.jsonl'])

if __name__=='__main__':unittest.main()
