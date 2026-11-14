from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.config import load_settings
from app.main import create_app
from app.core.contracts.models import BookingStatus, CalendarDay
from app.ports.pms import PmsError
from fastapi.testclient import TestClient

RESULTS = []

@contextmanager
def app(**env):
    with tempfile.TemporaryDirectory(prefix='f2-audit-db-') as directory, patch.dict(os.environ, env):
        settings = load_settings(env_file=None, database_path=Path(directory)/'audit.db',
            pms_provider='mock', llm_provider='mock', market_provider='mock', reviews_provider='mock',
            fixed_today=date(2026,6,1), handler_timeout_s=10, sim_wait_timeout_s=20,
            webhook_token=None, disabled_features=[])
        with TestClient(create_app(settings)) as client:
            yield client, client.app.state.container

def booking(client, name):
    return next(item for item in client.get('/bookings').json() if item['guest_name']==name)

def prop(client, name):
    return next(item for item in client.get('/properties').json() if item['name']==name)

def report(client, property_id, description='The air conditioning is broken', booking_id=None):
    body = dict(property_id=property_id, description=description)
    if booking_id: body['booking_id'] = booking_id
    response = client.post('/incidents/report', params={'wait':True}, json=body)
    assert response.status_code == 200, response.text
    return next(item for item in client.get('/incidents').json() if item['property_id']==property_id and item['description']==description)

def approve(client, approval_id, option='relocate'):
    response = client.post(f'/approvals/{approval_id}/decision', params={'wait':True}, json={'decision':'approve','option':option})
    assert response.status_code==200, response.text
    return response.json()

def service(c):
    return next(sub.handler.__self__ for sub in c.bus.subscriptions() if sub.name=='f2_incidents.on_issue_reported')

def outbound(client, booking_id):
    return [item for item in client.get(f'/bookings/{booking_id}/messages').json() if item['direction']=='outbound']

def add(name, discrepancy, **observed):
    RESULTS.append(dict(case=name, discrepancy=discrepancy, observed=observed))

def mismatch_report():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        p=prop(client,'Old Town Studio'); b=booking(client,'Lukas Mueller')
        inc=report(client,p['id'],booking_id=b['id'])
        approval=c.approvals.get(inc['approval_id'])
        add('booking_property_mismatch', inc['property_id']!=b['property_id'] and inc['booking_id']==b['id'],
            incident_property=p['name'], affected_guest=b['guest_name'], guest_actual_property=c.world.get_property(b['property_id']).name,
            approval_summary=approval.summary)

def changed_capacity():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id'])
        approve(client,inc['approval_id'])
        target=c.world.get_property(inc['relocation_property_id'])
        c.world.upsert_property(target.model_copy(update={'max_guests':1,'bedrooms':0,'city':'Other city'}))
        response=client.post(f"/incidents/{inc['id']}/guest-answer",params={'wait':True},json={'choice':'accept'})
        now=client.get(f"/incidents/{inc['id']}").json()
        add('changed_target_compatibility', now['status']=='relocated', response=response.status_code,status=now['status'],guest_count=b['guest_count'],target_capacity=1,target_city='Other city',messages=[m['body'] for m in outbound(client,b['id'])])

def failed_calendar_write():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id']); approve(client,inc['approval_id'])
        async def fail(*args): raise PmsError('simulated calendar write failure')
        with patch.object(c.calendar,'set_availability',fail):
            client.post(f"/incidents/{inc['id']}/guest-answer",params={'wait':True},json={'choice':'accept'})
        now=client.get(f"/incidents/{inc['id']}").json()
        days=client.portal.call(c.calendar.get_days,inc['relocation_property_id'],date(2026,6,1),date(2026,6,4))
        add('failed_block_still_confirms',now['status']=='relocated' and all(d.available for d in days),status=now['status'],target_still_available=[d.available for d in days],messages=[m['body'] for m in outbound(client,b['id'])],notifications=[n['body'] for n in client.get('/notifications').json() if 'being relocated' in n['title']])

def invalid_calendar():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        async def malformed(property_id,start,end):
            return [CalendarDay(property_id='wrong-property',day=start,available=True,price=1) for _ in range((end-start).days)]
        with patch.object(c.calendar,'get_days',malformed):
            result=client.portal.call(service(c)._is_free,'a-target',date(2026,6,1),date(2026,6,4))
            b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id'])
        add('invalid_calendar_false_free',result,method_returns_free=result,relocation_offered=inc['relocation_property_id'] is not None)

def concurrent_moves():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        lukas=booking(client,'Lukas Mueller'); emma=booking(client,'Emma Johnson')
        eb=c.world.get_booking(emma['id']); c.world.upsert_booking(eb.model_copy(update={'check_in':date(2026,5,31),'check_out':date(2026,6,4)}))
        first=report(client,lukas['property_id'],'The air conditioning is broken')
        second=report(client,emma['property_id'],'There is mold in the bathroom')
        assert first['relocation_property_id']==second['relocation_property_id'],(first,second)
        approve(client,first['approval_id']); approve(client,second['approval_id'])
        target=first['relocation_property_id']; original=c.calendar.get_days
        async def both_accept():
            gate=asyncio.Event(); count=0
            async def racing(property_id,start,end):
                nonlocal count
                days=await original(property_id,start,end)
                if property_id==target:
                    count+=1
                    if count<=2:
                        if count==2: gate.set()
                        await asyncio.wait_for(gate.wait(),3)
                return days
            with patch.object(c.calendar,'get_days',racing):
                await asyncio.gather(service(c).answer_relocation(first['id'],'accept'),service(c).answer_relocation(second['id'],'accept'))
                assert await c.bus.join(timeout=10)
            return count
        reads=client.portal.call(both_accept)
        states=[client.get(f"/incidents/{i['id']}").json()['status'] for i in (first,second)]
        add('concurrent_relocations_same_target',states==['relocated','relocated'],states=states,target=c.world.get_property(target).name,guests=[lukas['guest_name'],emma['guest_name']],calendar_checks=reads)

def offer_failure():
    with app() as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id']); connection=c.pms.get('mock'); original=connection.send_message; calls=0
        async def fail_first(*args):
            nonlocal calls
            calls+=1
            if calls==1: raise PmsError('offer transport failure')
            return await original(*args)
        with patch.object(connection,'send_message',fail_first): approve(client,inc['approval_id'])
        now=client.get(f"/incidents/{inc['id']}").json()
        messages=outbound(client,b['id'])
        add('failed_offer_still_auto_relocates',now['status']=='relocated' and [m['status'] for m in messages]==['failed','sent'],status=now['status'],messages=[dict(status=m['status'],body=m['body']) for m in messages],offered_notification=any('Relocation offered' in n['title'] for n in client.get('/notifications').json()))

def f3_after_relocation():
    with app() as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id']); approve(client,inc['approval_id'])
        target=c.world.get_property(inc['relocation_property_id'])
        client.post('/sim/guest-message',params={'wait':True},json={'booking_id':b['id'],'text':'What is the address of my apartment?'})
        last=outbound(client,b['id'])[-1]['body']
        add('documented_relocation_internal_state_gap',target.address not in last and c.world.get_property(b['property_id']).address in last,status=client.get(f"/incidents/{inc['id']}").json()['status'],confirmed_target=target.address,subsequent_copilot_reply=last,documented=True)

def chat_acceptance_not_connected():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id']); approve(client,inc['approval_id'])
        response=client.post('/sim/guest-message',params={'wait':True},json={'booking_id':b['id'],'text':'Yes, I accept the relocation. Please move us to the offered apartment.'})
        now=client.get(f"/incidents/{inc['id']}").json()
        add('documented_chat_acceptance_not_wired',now['status']=='awaiting_guest' and now['guest_answer'] is None,status=now['status'],guest_answer=now['guest_answer'],f3_reply=outbound(client,b['id'])[-1]['body'],source_response=response.json(),documented=True)

def f3_cancelled_before_decision():
    with app() as (client,c):
        b=booking(client,'Emma Johnson')
        client.post('/sim/guest-message',params={'wait':True},json={'booking_id':b['id'],'text':'Could we check in early at noon?'})
        approval=next(a for a in client.get('/approvals').json() if a['kind']=='guest_request')
        c.world.upsert_booking(c.world.get_booking(b['id']).model_copy(update={'status':BookingStatus.CANCELLED}))
        before=len(outbound(client,b['id'])); approve(client,approval['id'],'approve'); after=len(outbound(client,b['id']))
        add('positive_f3_cancellation_guard',False,passed=before==after,outbound_before=before,outbound_after=after,manual_warning=any('Host decision' in n['body'] for n in client.get('/notifications').json()))

def repeat_decisions():
    with app() as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id']); first=approve(client,inc['approval_id']); count=len(outbound(client,b['id'])); second=approve(client,inc['approval_id']); after=len(outbound(client,b['id']))
        add('positive_f2_decision_idempotence',False,passed=first['applied'] and not second['applied'] and count==after,first_applied=first['applied'],second_applied=second['applied'],messages_before=count,messages_after=after)

def hidden_closure_message():
    from streamlit.testing.v1 import AppTest
    from ui import api_client
    with app() as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id'],'Water is leaking under the kitchen sink.'); approve(client,inc['approval_id'],'remediate')
        closure=next(a for a in client.get('/approvals').json() if a['kind']=='incident_closure')
        description=closure['options'][0]['description']; reviewed=description.split('"')[1]
        def get(path,**params): return client.get(path,params=params).json()
        with patch.object(api_client,'get',get):
            page=AppTest.from_file(str(ROOT/'ui/views/approvals.py')).run()
        displayed='\n'.join(str(x.value) for x in page.markdown)
        add('single_option_hides_reviewed_guest_text',reviewed not in displayed,api_option_description=description,summary_contains_exact_text=reviewed in closure['summary'],ui_displays_exact_text=reviewed in displayed,ui_exceptions=[str(x.value) for x in page.exception])

def stale_closure():
    with app() as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id'],'Water is leaking under the kitchen sink.'); approve(client,inc['approval_id'],'remediate')
        closure=next(a for a in client.get('/approvals').json() if a['kind']=='incident_closure')
        response=client.post('/incidents/report',params={'wait':True},json={'property_id':b['property_id'],'description':'Water is still leaking under the kitchen sink after the repair.'})
        assert response.status_code==200
        before=client.get(f"/incidents/{inc['id']}").json()
        approve(client,closure['id'],'close')
        after=client.get(f"/incidents/{inc['id']}").json()
        add('old_closure_after_new_unresolved_report',after['status']=='resolved',reports_before_close=before['report_count'],closure_unchanged=before['closure_approval_id']==closure['id'],closure_summary=closure['summary'],final_status=after['status'],final_guest_reply=outbound(client,b['id'])[-1]['body'])

def target_has_open_incident():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        target=prop(client,'Herastrau Family Flat'); bad=report(client,target['id'],'There is mold in the bathroom')
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id'],'The air conditioning is broken')
        add('relocation_to_known_broken_property',inc['relocation_property_id']==target['id'],known_target_incident_status=bad['status'],known_target_problem=bad['description'],offered_target=c.world.get_property(inc['relocation_property_id']).name)

def distinct_leaks_merge():
    with app(F2_SIM_GUEST_ANSWER='off') as (client,c):
        b=booking(client,'Lukas Mueller'); first=report(client,b['property_id'],'Water is leaking under the kitchen sink.')
        response=client.post('/incidents/report',params={'wait':True},json={'property_id':b['property_id'],'description':'The bathroom shower is leaking badly.'})
        incidents=client.get('/incidents').json()
        add('distinct_locations_same_category_merge',len(incidents)==1 and incidents[0]['report_count']==2,incident_count=len(incidents),report_count=incidents[0]['report_count'],reports=[r['description'] for r in incidents[0]['reports']],approval_count=len(client.get('/approvals').json()))

def deadline_publication_failure():
    from app.features.f2_incidents.events import IncidentTimeout
    from app.core.clock import utcnow
    with app(F2_SIM_ENABLED='false') as (client,c):
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id']); svc=service(c)
        svc.repo.update(inc['id'],staff_deadline=utcnow()-timedelta(minutes=1))
        original=c.bus.publish
        async def fail_timeout(event):
            if isinstance(event,IncidentTimeout): raise RuntimeError('simulated timeout publication failure')
            return await original(event)
        with patch.object(c.bus,'publish',fail_timeout):
            try: client.portal.call(svc.check_deadlines)
            except RuntimeError: pass
        retry=client.portal.call(svc.check_deadlines)
        now=svc.repo.get(inc['id'])
        add('timeout_lost_on_publication_failure',now.staff_deadline is None and now.status=='awaiting_staff' and retry==0,status=now.status,deadline=now.staff_deadline,retry_events=retry)

def concurrent_duplicate_stress():
    with app() as (client,c):
        b=booking(client,'Lukas Mueller'); svc=service(c)
        async def batch():
            await asyncio.gather(*(svc.report_issue(property_id=b['property_id'],description='Water is leaking under the kitchen sink.',staff_id=None,booking_id=b['id'],severity='medium') for _ in range(20)))
            return await c.bus.join(timeout=20)
        completed=client.portal.call(batch)
        incidents=client.get('/incidents').json(); approvals=client.get('/approvals').json(); errors=[h for e in c.bus.list_events(limit=2000) for h in e['handlers'] if h['status']!='ok']
        add('positive_concurrent_duplicate_stress',False,passed=completed and len(incidents)==1 and incidents[0]['report_count']==20 and len(approvals)==1 and not errors,incident_count=len(incidents),report_count=incidents[0]['report_count'],approval_count=len(approvals),handler_errors=errors,foreign_key_errors=c.db.query('PRAGMA foreign_key_check'))

def full_llm_failure_fallback():
    from app.ports.llm import LLMError
    class Failing:
        name='audit-failure'
        async def chat(self,request): raise LLMError('simulated LLM outage')
    with app() as (client,c):
        original=c.agent._llm
        c.agent._llm=Failing()
        b=booking(client,'Lukas Mueller'); inc=report(client,b['property_id'],'The air conditioning is broken'); approve(client,inc['approval_id'])
        closure=next(a for a in client.get('/approvals').json() if a['kind']=='incident_closure')
        approve(client,closure['id'],'close'); now=client.get(f"/incidents/{inc['id']}").json()
        c.agent._llm=original
        client.post('/sim/guest-message',params={'wait':True},json={'booking_id':b['id'],'text':'What time is check-out?'})
        errors=[h for e in c.bus.list_events(limit=2000) for h in e['handlers'] if h['status']!='ok']
        add('positive_full_f2_llm_outage_and_f3_recovery',False,passed=now['status']=='resolved' and len(outbound(client,b['id']))==3 and not errors,final_status=now['status'],drafts_source=now['drafts_source'],closure_summary_fallback='AI summary was not available' in closure['summary'],f3_after_recovery=outbound(client,b['id'])[-1]['body'],handler_errors=errors)

if __name__=='__main__':
    for test in (mismatch_report,changed_capacity,failed_calendar_write,invalid_calendar,concurrent_moves,offer_failure,f3_after_relocation,chat_acceptance_not_connected,f3_cancelled_before_decision,repeat_decisions,hidden_closure_message,stale_closure,target_has_open_incident,distinct_leaks_merge,deadline_publication_failure,concurrent_duplicate_stress,full_llm_failure_fallback):
        try: test()
        except Exception as exc: RESULTS.append(dict(case=test.__name__,error=f'{type(exc).__name__}: {exc}'))
    print(json.dumps(RESULTS,indent=2,ensure_ascii=True))
