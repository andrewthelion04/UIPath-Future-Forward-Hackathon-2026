"""Read-only F4 integration audit: mock ports, temporary databases, no live keys."""
from __future__ import annotations
import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.container import Container
from app.core.agent.tools import RunContext
from app.core.clock import utcnow
from app.core.contracts.events import ReviewAnalysisRequested
from app.core.contracts.models import Review, ReviewBatch
from app.features.f4_review_analysis.analysis import build_tools, quote_found
from app.ports.llm import ChatMessage, ChatResponse, ToolCall, Usage
from app.adapters.apify.reviews import ApifyReviewsAdapter

TEXT = 'The wifi was unusable. The room was not dirty.'

class Reviews:
    name = 'audit'
    def __init__(self, wrong=False): self.wrong = wrong
    async def fetch_reviews(self, property_id):
        target = 'property_other' if self.wrong else property_id
        return ReviewBatch(property_id=property_id if self.wrong=='items_only' else target, source=self.name, fetched_at=utcnow(), reviews=[
            Review(id='audit-review', property_id=target, source=self.name, rating=2,
                   text=TEXT, reviewed_on=date(2026,5,1))])

class GroundedLLM:
    name = 'audit-grounded'
    async def chat(self, request):
        done = [c.name for m in request.messages for c in m.tool_calls]
        if 'add_recommendation' not in done:
            calls = [ToolCall.make('add_recommendation', theme='Wi-Fi', priority='high',
                                  action='Upgrade the unreliable Wi-Fi router.',
                                  quotes=['The wifi was unusable.'], reason='Use source evidence')]
        else:
            calls = [ToolCall.make('finish', result='One verified recommendation.', evidence=[], reason='All work finished')]
        return ChatResponse(message=ChatMessage(role='assistant', tool_calls=calls), model=self.name, usage=Usage())

class ConcurrentLLM(GroundedLLM):
    def __init__(self): self.started=0; self.gate=asyncio.Event()
    async def chat(self, request):
        done = [c.name for m in request.messages for c in m.tool_calls]
        if 'add_recommendation' not in done:
            self.started += 1
            if self.started >= 2: self.gate.set()
            await asyncio.wait_for(self.gate.wait(), timeout=3)
        return await super().chat(request)

class BypassLLM:
    name='audit-finish-bypass'
    async def chat(self, request):
        return ChatResponse(message=ChatMessage(role='assistant', tool_calls=[ToolCall.make(
            'finish', result='Replace all mattresses immediately: 20 guests reported bed bugs.',
            evidence=['Every bed has bugs - a quote nobody wrote.'], reason='Skip all guarded tools')]),
            usage=Usage(), model=self.name)

def settings(path):
    return load_settings(env_file=None, database_path=path, fixed_today=date(2026,6,1),
                         pms_provider='mock',llm_provider='mock',reviews_provider='mock',market_provider='mock',
                         disabled_features=[], webhook_token=None,handler_timeout_s=10,sim_wait_timeout_s=15)

async def case(name, *, reviews=None, llm=None, concurrent=False):
    with tempfile.TemporaryDirectory(prefix='staypilot-f4-audit-') as folder:
        overrides={'reviews':reviews or Reviews()}
        if llm: overrides['llm']=llm
        c=Container(settings(Path(folder)/'test.db'),overrides=overrides)
        await c.startup()
        try:
            prop=next(p for p in c.world.list_properties() if p.name=='Old Town Studio')
            await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
            if concurrent: await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
            assert await c.bus.join(timeout=8)
            result={
                'case':name,'property_id':prop.id,
                'snapshots':c.db.query('SELECT id,property_id FROM f4_review_snapshots'),
                'insights':c.db.query('SELECT id,property_id,snapshot_id,recommendations,evidence FROM f4_insights'),
                'recommendations':c.db.query('SELECT theme,action,quotes FROM f4_recommendations'),
                'notification_titles':[n.title for n in c.notifications.list()],
                'f4_runs':len(c.agent_trace.list_runs(feature='f4_review_analysis')),
            }
            return result
        finally: await c.shutdown()

async def tool_cases():
    reviews=[Review(id='r',property_id='p',rating=2,text=TEXT,source='audit')]
    tool=next(t for t in build_tools() if t.name=='add_recommendation')
    run=RunContext('audit','audit',{'reviews':reviews,'recommendations':[]})
    args=tool.args_model(theme='  ',priority='high',action='          ',quotes=['wifi was unusable'],reason='Boundary test')
    await tool.handler(args,run)
    return {'case':'blank_recommendation_and_reversed_quote','stored':[r.model_dump() for r in run.state['recommendations']],
            'reversed_quote_accepted':quote_found('dirty ... not',reviews)}

class ReorderedReviews:
    name='audit-order'
    def __init__(self): self.reversed=False; self.fail=False
    async def fetch_reviews(self,property_id):
        if self.fail: raise RuntimeError('controlled source failure after restart')
        reviews=[Review(id='r1',property_id=property_id,source=self.name,rating=2,text=TEXT),
                 Review(id='r2',property_id=property_id,source=self.name,rating=3,text='The heating was cold.')]
        return ReviewBatch(property_id=property_id,source=self.name,fetched_at=utcnow(),
                           reviews=list(reversed(reviews)) if self.reversed else reviews)

async def restart_cache_case():
    with tempfile.TemporaryDirectory(prefix='staypilot-f4-restart-audit-') as folder:
        path=Path(folder)/'test.db'
        source=ReorderedReviews()
        c=Container(settings(path),overrides={'reviews':source})
        await c.startup()
        prop=next(p for p in c.world.list_properties() if p.name=='Old Town Studio')
        await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
        assert await c.bus.join(timeout=8)
        await c.shutdown()
        source.reversed=True
        c=Container(settings(path),overrides={'reviews':source})
        await c.startup()
        try:
            await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
            assert await c.bus.join(timeout=8)
            unchanged={table:c.db.query_one(f'SELECT COUNT(*) AS total FROM {table}')["total"]
                       for table in ('f4_review_snapshots','f4_insights','agent_runs')}
            assert unchanged=={'f4_review_snapshots':1,'f4_insights':1,'agent_runs':1}
            assert c.notifications.list()[0].title=='No new reviews for Old Town Studio'
            source.fail=True
            await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
            assert await c.bus.join(timeout=8)
            assert c.db.query_one('SELECT used_fallback FROM f4_insights ORDER BY rowid DESC LIMIT 1')['used_fallback']==1
            assert c.notifications.list()[0].level=='warning'
            return {'case':'healthy_restart_reordered_cache_then_source_failure',
                    'after_unchanged':unchanged,'fallback_after_restart':True,
                    'snapshots_after_fallback':c.db.query_one('SELECT COUNT(*) AS n FROM f4_review_snapshots')['n']}
        finally: await c.shutdown()

async def apify_payload_case():
    with tempfile.TemporaryDirectory(prefix='staypilot-f4-adapter-audit-') as folder:
        c=Container(settings(Path(folder)/'test.db'))
        await c.startup()
        adapter=None
        try:
            prop=next(p for p in c.world.list_properties() if p.name=='Old Town Studio')
            listing='https://www.airbnb.com/rooms/AUDIT-A'
            other='https://www.airbnb.com/rooms/AUDIT-B'
            c.ids.link('airbnb','listing',listing,prop.id)
            # Fixture setup yields so the unrelated F1 mapping replay drains before teardown.
            await asyncio.sleep(0)
            payload=[{'id':'other-property-review','startUrl':other,'rating':1,'text':'Pests at another property.'},
                     {'id':'duplicate','startUrl':listing,'rating':True,'text':'Boolean rating is not a star score.'},
                     {'id':'duplicate','startUrl':listing,'rating':True,'text':'Boolean rating is not a star score.'}]
            adapter=ApifyReviewsAdapter(token='audit-no-network',actor_id='audit/actor',
                     input_template='{"startUrls":[{"url":"{listing_url}"}]}',ids=c.ids,
                     transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload)))
            batch=await adapter.fetch_reviews(prop.id)
            return {'case':'apify_malformed_and_cross_listing_payload','requested_listing':listing,
                    'source_listing_of_first_item':other,'accepted_reviews':[r.model_dump(mode='json') for r in batch.reviews],
                    'count':len(batch.reviews),'unique_ids':len({r.id for r in batch.reviews})}
        finally:
            if adapter is not None: await adapter.aclose()
            await c.shutdown()

async def out_of_order_case():
    class ChangingReviews:
        name='audit-changing'
        def __init__(self):self.calls=0
        async def fetch_reviews(self,property_id):
            self.calls+=1
            text='The wifi was unusable.' if self.calls==1 else 'The street was noisy at night.'
            return ReviewBatch(property_id=property_id,source=self.name,fetched_at=utcnow(),reviews=[
                Review(id='current-review',property_id=property_id,source=self.name,rating=2,text=text)])
    with tempfile.TemporaryDirectory(prefix='staypilot-f4-order-audit-') as folder:
        c=Container(settings(Path(folder)/'test.db'),overrides={'reviews':ChangingReviews()})
        await c.startup()
        try:
            prop=next(p for p in c.world.list_properties() if p.name=='Old Town Studio')
            original=c.agent.run
            entered,release=asyncio.Event(),asyncio.Event()
            started=0
            async def run(spec,**kwargs):
                nonlocal started
                started+=1
                first=started==1
                if first:
                    entered.set()
                    await release.wait()
                result=await original(spec,**kwargs)
                if not first:release.set()
                return result
            c.agent.run=run
            await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
            await asyncio.wait_for(entered.wait(),timeout=3)
            await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
            assert await c.bus.join(timeout=8)
            rows=c.db.query('SELECT recommendations,snapshot_id FROM f4_insights ORDER BY rowid DESC')
            latest_snapshot=c.db.query_one('SELECT id,reviews FROM f4_review_snapshots ORDER BY fetched_at DESC LIMIT 1')
            await c.bus.publish(ReviewAnalysisRequested(property_id=prop.id))
            assert await c.bus.join(timeout=8)
            return {'case':'concurrent_old_analysis_finishes_after_new',
                    'insights_newest_first':rows,'latest_snapshot':latest_snapshot,
                    'third_request_notification':c.notifications.list()[0].body,
                    'f4_runs_after_third_request':started}
        finally: await c.shutdown()

async def main():
    results=[]
    results.append(await case('concurrent_same_property',llm=ConcurrentLLM(),concurrent=True))
    results.append(await case('finish_bypasses_recommendation_validation',llm=BypassLLM()))
    results.append(await case('wrong_property_source_cross_contaminates',reviews=Reviews(wrong=True),llm=GroundedLLM()))
    results.append(await case('wrong_property_review_items_cross_contaminate',reviews=Reviews(wrong='items_only'),llm=GroundedLLM()))
    results.append(await tool_cases())
    results.append(await restart_cache_case())
    results.append(await apify_payload_case())
    results.append(await out_of_order_case())
    print(json.dumps(results,indent=2))

if __name__ == '__main__':
    asyncio.run(main())
