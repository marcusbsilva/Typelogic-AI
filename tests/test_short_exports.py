from typelogic.exports import export_filename
from typelogic.store import Store
from test_cli_and_deletion import cli
import re,json

def test_names_are_stable_eight_character_base32():
 for id in ['0'*32,'1'*32,'example-id']:
  full=export_filename(id);lite=export_filename(id,'lite')
  assert re.fullmatch('[a-z2-7]{8}\\.json',full)
  assert full==export_filename(id) and full!=lite

def test_cli_generated_filename(tmp_path):
 data=tmp_path/'data';db=Store(data/'typelogic.sqlite3');id=db.add_decision({}, {'answers':{}},'Test','default');assert db.stats()['decisions']==1;db.close()
 output=tmp_path/'exports';args=('export','--data',str(data),'--id',id,'--output-dir',str(output))
 result=cli(tmp_path,*args);assert result.returncode==0,result.stderr
 value=json.loads((output/export_filename(id)).read_text());assert value['id']==id and value['title']=='Test' and 'status' not in value
 assert cli(tmp_path,*args).returncode==1
 assert cli(tmp_path,'export','--data',str(data),'--output-dir',str(output)).returncode==1
 bad=tmp_path/'should-not-exist.json';assert cli(tmp_path,*args,'--output',str(bad)).returncode==1
 assert not bad.exists()
