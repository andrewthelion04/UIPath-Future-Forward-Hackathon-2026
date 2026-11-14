"""Reproducere de audit: confirma comportamentele observate, fara modificari de productie."""

from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import json, sys, tempfile, asyncio, csv
from pathlib import Path
from datetime import date, datetime, UTC
from conftest import make_settings, app_client, booking_by_guest, approve, drain
from app.core.contracts.events import ApprovalDecided

results = []
def setup(temp):
    return app_client(make_settings(Path(temp)))
def cancel(client):
    booking = booking_by_guest(client, 'Emma Johnson')
    answer = client.post('/sim/cancel-booking', params={'wait':True}, json={'booking_id':booking['id']})
    assert answer.status_code == 200 and answer.json()['completed']
    proposal, = client.get('/f5/proposals').json()
    return booking,proposal

def rates(client, booking):
    return client.get(f"/properties/{booking['property_id']}/calendar",params={'start':booking['check_in'],'days':3}).json()

with tempfile.TemporaryDirectory(prefix='f5-rule-repro-') as td:
    with setup(td) as client:
        b,p=cancel(client); c=client.app.state.container
        before=rates(client,b)
        host=c.world.get_host(); host.rules.max_relist_discount_pct=0; c.world.upsert_host(host)
        response=approve(client,p['approval_id'])
        after=rates(client,b); pafter=client.get('/f5/proposals').json()[0]
        assert pafter['status']=='applied'
        assert any(a['price'] < z['price'] for a,z in zip(after,before))
        results.append({'case':'changed_discount_policy','max_discount_now':0,'before':[x['price'] for x in before],'after':[x['price'] for x in after],'proposal_status':pafter['status'],'approval_status':response['approval']['status'] if 'approval' in response else response})

with tempfile.TemporaryDirectory(prefix='f5-forged-decision-') as td:
    with setup(td) as client:
        b,p=cancel(client); c=client.app.state.container
        before=rates(client,b)
        event=ApprovalDecided(approval_id=p['approval_id'],feature='f5_relisting',kind='relist_price',decision='approved',option='approve',payload={'proposal_id':p['id']})
        client.portal.call(c.bus.publish,event); assert drain(client)
        after=rates(client,b)
        actual=c.approvals.get(p['approval_id'])
        assert actual.status=='pending' and client.get('/f5/proposals').json()[0]['status']=='applied'
        assert any(a['price']<z['price'] for a,z in zip(after,before))
        results.append({'case':'decision_event_without_persisted_approval','approval_status':actual.status,'proposal_status':'applied','before':[x['price'] for x in before],'after':[x['price'] for x in after]})

with tempfile.TemporaryDirectory(prefix='f5-deadline-rule-') as td:
    with setup(td) as client:
        b,p=cancel(client); c=client.app.state.container
        host=c.world.get_host(); host.rules.check_in_time='11:00'; c.world.upsert_host(host)
        c.clock.today=lambda:date(2026,6,3)
        c.clock.now=lambda:datetime(2026,6,3,8,30,tzinfo=UTC)
        before=rates(client,b); approve(client,p['approval_id']); after=rates(client,b)
        pafter=client.get('/f5/proposals').json()[0]
        assert pafter['status']=='applied'
        results.append({'case':'host_checkin_time_earlier_after_proposal','checkin_time_now':'11:00','now_local':'2026-06-03T11:30+03:00','proposal_expiry':p['expires_at'],'proposal_status':pafter['status'],'before':[x['price'] for x in before],'after':[x['price'] for x in after]})

print(json.dumps(results,indent=2))
