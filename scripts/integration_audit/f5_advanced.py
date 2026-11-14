"""Reproducere de audit: confirma comportamentele observate, fara modificari de productie."""

from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import json, sys, tempfile, asyncio
from pathlib import Path
from datetime import date
from conftest import make_settings, app_client, booking_by_guest, approve, property_by_name

results=[]
def cancel(client):
 b=booking_by_guest(client,'Emma Johnson'); r=client.post('/sim/cancel-booking',params={'wait':True},json={'booking_id':b['id']}); assert r.json()['completed']; return b,client.get('/f5/proposals').json()[0]
def days(client,b):
 return client.get(f"/properties/{b['property_id']}/calendar",params={'start':b['check_in'],'days':3}).json()

with tempfile.TemporaryDirectory(prefix='f5-aggregate-timeout-') as td:
 with app_client(make_settings(Path(td),handler_timeout_s=.15,sim_wait_timeout_s=2)) as client:
  b,p=cancel(client); c=client.app.state.container; original=c.calendar.set_price
  async def slow_write(*args):
   await asyncio.sleep(.09); await original(*args)
  c.calendar.set_price=slow_write
  response=approve(client,p['approval_id'])
  ps=client.get('/f5/proposals').json()[0]
  prices=[d['price'] for d in days(client,b)]
  assert ps['status']=='awaiting_approval' and c.approvals.get(p['approval_id']).status=='approved'
  assert prices==[65.96,70,80.5], prices
  repeated=approve(client,p['approval_id'])
  handler=c.db.query_one("SELECT status,error FROM event_handler_runs WHERE feature='f5_relisting' AND status='timeout' ORDER BY id DESC LIMIT 1")
  assert handler and repeated['applied'] is False
  results.append({'case':'aggregate_parent_timeout_partial_apply','prices':prices,'proposal_status':ps['status'],'approval_status':'approved','handler':handler,'repeated_decision_applied':repeated['applied'],'notifications':[{'title':n['title'],'body':n['body']} for n in client.get('/notifications').json()[:2]]})

with tempfile.TemporaryDirectory(prefix='f5-f2-race-') as td:
 with app_client(make_settings(Path(td),handler_timeout_s=10,sim_wait_timeout_s=15)) as client:
  b,p=cancel(client); c=client.app.state.container
  lukas=booking_by_guest(client,'Lukas Mueller'); other=property_by_name(client,'Herastrau Family Flat')
  client.portal.call(c.calendar.set_availability,other['id'],date.fromisoformat(lukas['check_in']),date.fromisoformat(lukas['check_out']),False)
  result=client.post('/sim/staff-report',params={'wait':True},json={'property_id':lukas['property_id'],'booking_id':lukas['id'],'description':'The air conditioning is not working','staff_id':'staff_maria'})
  assert result.json()['completed']
  incident,=client.get('/incidents').json()
  assert incident['relocation_property_id']==b['property_id'],incident
  iapproval=incident['approval_id']
  original_get=c.calendar.get_days; original_set=c.calendar.set_availability
  async def run_race():
   entered,resume,blocked=asyncio.Event(),asyncio.Event(),asyncio.Event()
   async def read(property_id,start,end):
    output=await original_get(property_id,start,end)
    if property_id==b['property_id'] and start==date.fromisoformat(b['check_in']) and end==date.fromisoformat(b['check_out']):
     entered.set(); await resume.wait()
    return output
   async def availability(property_id,start,end,available):
    await original_set(property_id,start,end,available)
    if property_id==b['property_id'] and available is False: blocked.set()
   c.calendar.get_days=read; c.calendar.set_availability=availability
   await c.approvals.decide(p['approval_id'],decision='approve')
   await asyncio.wait_for(entered.wait(),2)
   await c.approvals.decide(iapproval,decision='approve',option='relocate')
   await asyncio.wait_for(blocked.wait(),2)
   resume.set(); assert await c.bus.join(10)
  client.portal.call(run_race)
  c.calendar.get_days=original_get
  ps=client.get('/f5/proposals').json()[0]; actual=days(client,b); incident=client.get('/incidents').json()[0]
  assert ps['status']=='applied' and actual[0]['available'] is False and actual[0]['price']==65.96
  results.append({'case':'f2_relocation_after_f5_calendar_read','proposal_status':ps['status'],'incident_status':incident['status'],'first_night':actual[0],'note':'F2 blocked the first night after F5 free snapshot; F5 then discounted the occupied night. Known port no-CAS limitation extends to local cross-feature operations.'})

print(json.dumps(results,indent=2))
