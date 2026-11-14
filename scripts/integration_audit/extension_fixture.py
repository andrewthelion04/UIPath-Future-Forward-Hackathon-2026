"""Reproducere de audit: confirma comportamentele observate, fara modificari de productie."""

from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import json,sys,tempfile
from pathlib import Path
from conftest import make_settings,app_client,booking_by_guest
with tempfile.TemporaryDirectory(prefix='extension-disabled-f2-') as td:
 with app_client(make_settings(Path(td),disabled_features=['f2_incidents'])) as client:
  b=booking_by_guest(client,'Emma Johnson')
  response=client.post('/sim/guest-message',params={'wait':True},json={'booking_id':b['id'],'text':'Could we check in early at noon?'})
  assert response.status_code==200 and response.json()['completed']
  fixture={}
  for key,route in [('approvals','/approvals'),('notifications','/notifications'),('properties','/properties'),('incidents','/incidents')]:
   result=client.get(route);fixture[key]={'status':result.status_code,'body':result.json()}
  assert fixture['incidents']['status']==404 and len(fixture['approvals']['body'])==1 and len(fixture['properties']['body'])==4
  Path(tempfile.gettempdir(),'audit_extension_fixture.json').write_text(json.dumps(fixture),encoding='utf-8')
print(json.dumps({key:{'status':value['status'],'items':len(value['body']) if isinstance(value['body'],list) else None} for key,value in fixture.items()}))
