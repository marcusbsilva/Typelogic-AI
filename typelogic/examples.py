"""Fictitious business cases shared by the CLI and local UI."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'examples/business'
def catalogue():
    rows=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
    return {'examples':[{**row,'payload':json.loads((ROOT/row['file']).read_text(encoding='utf-8'))} for row in rows],'fictitious':True}

