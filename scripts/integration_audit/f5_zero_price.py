"""Reproducere de audit: confirma comportamentele observate, fara modificari de productie."""

from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import sys, tempfile, os,json
from pathlib import Path
from datetime import date,timedelta
from conftest import make_settings,app_client,booking_by_guest,approve
from app.core.contracts.models import Comparable
class Market:
 name='audit';attribution=None
 async def find_comparables(self,q):
  return [Comparable(ref='penny',title='Valid cheap comparable',city=q.city,bedrooms=q.bedrooms,max_guests=q.max_guests,amenities=q.amenities,nightly_price=.01,currency='EUR',source='audit')]
previous=os.environ.get('F5_UNDERCUT_PCT');os.environ['F5_UNDERCUT_PCT']='99.9'
try:
 with tempfile.TemporaryDirectory(prefix='f5-zero-price-') as td:
  with app_client(make_settings(Path(td)),market=Market()) as client:
   c=client.app.state.container;b=booking_by_guest(client,'Emma Johnson')
   host=c.world.get_host();host.rules.max_relist_discount_pct=100;c.world.upsert_host(host)
   start=date.fromisoformat(b['check_in']);client.portal.call(c.calendar.set_price,b['property_id'],start,start+timedelta(days=1),.01)
   assert client.post('/sim/cancel-booking',params={'wait':True},json={'booking_id':b['id']}).json()['completed']
   proposal,=client.get('/f5/proposals').json()
   assert proposal['nightly_prices'][0]['original_price']==.01 and proposal['nightly_prices'][0]['proposed_price']==0
   approve(client,proposal['approval_id'])
   actual=client.get(f"/properties/{b['property_id']}/calendar",params={'start':b['check_in'],'days':3}).json()
   final=client.get('/f5/proposals').json()[0]
   assert final['status']=='applied' and actual[0]['price']==0
   print(json.dumps({'case':'positive_night_becomes_free','max_discount':100,'undercut':99.9,'base_price':proposal['base_price'],'comparable':proposal['comparable_price'],'schedule':proposal['nightly_prices'],'status':final['status'],'actual_prices':[x['price'] for x in actual]},indent=2))
finally:
 if previous is None:os.environ.pop('F5_UNDERCUT_PCT',None)
 else:os.environ['F5_UNDERCUT_PCT']=previous
