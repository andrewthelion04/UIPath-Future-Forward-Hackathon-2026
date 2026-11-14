"""Reproducere de audit: confirma comportamentele observate, fara modificari de productie."""

from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import json,sys,tempfile
from pathlib import Path
from conftest import make_settings,app_client,booking_by_guest,approve
with tempfile.TemporaryDirectory(prefix='extension-refresh-order-') as td:
 with app_client(make_settings(Path(td))) as client:
  b=booking_by_guest(client,'Emma Johnson')
  assert client.post('/sim/guest-message',params={'wait':True},json={'booking_id':b['id'],'text':'Could we check in early at noon?'}).json()['completed']
  def snapshot():
   return {k:client.get(route).json() for k,route in [('approvals','/approvals'),('notifications','/notifications'),('properties','/properties'),('incidents','/incidents')]}
  older=snapshot();approve(client,older['approvals'][0]['id']);newer=snapshot()
  assert len(older['approvals'])==1 and not newer['approvals']
  fixture={'older':older,'newer':newer,'normal_path_status':client.get('http://testserver/properties').status_code,'trailing_slash_concat_status':client.get('http://testserver//properties').status_code}
  assert fixture['normal_path_status']==200 and fixture['trailing_slash_concat_status']==404
  Path(tempfile.gettempdir(),'audit_extension_race_fixture.json').write_text(json.dumps(fixture),encoding='utf-8')
print(json.dumps({'older_approvals':len(older['approvals']),'newer_approvals':len(newer['approvals']),'normal_path_status':fixture['normal_path_status'],'trailing_slash_concat_status':fixture['trailing_slash_concat_status']}))
