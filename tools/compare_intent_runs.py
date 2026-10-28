"""Compare paired intent predictions; refuses mismatched cases, questions or catalogs."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from typelogic.benchmark_metrics import paired_comparison
from typelogic.learning import file_sha256


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--left',type=Path,required=True)
    p.add_argument('--right',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.out.exists():raise ValueError('Output exists')
    def read(path):return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]
    report=paired_comparison(read(a.left),read(a.right))
    report.update(left_sha256=file_sha256(a.left),right_sha256=file_sha256(a.right))
    a.out.parent.mkdir(parents=True,exist_ok=True)
    with a.out.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2,allow_nan=False)
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
