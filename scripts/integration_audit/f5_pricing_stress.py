"""Reproducere de audit: confirma comportamentele observate, fara modificari de productie."""

from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import sys, random, math,json
from datetime import date,timedelta
from app.features.f5_relisting.pricing import distribute_prices,floor_price
from app.core.contracts.models import CalendarDay
rng=random.Random(5005); count=20000;nights=0;violations=[]
for case in range(count):
 base=rng.randint(1,200000)/100;market=rng.randint(1,400000)/100
 discount=rng.choice([0,1,3,25,50,99,100,rng.random()*100]);undercut=rng.choice([0,3,99,99.9,rng.random()*99.9])
 rows=[CalendarDay(property_id='audit',day=date(2026,6,1)+timedelta(days=i),available=True,price=rng.randint(1,400000)/100,currency='EUR') for i in range(rng.randint(1,14))]
 for row,n in zip(rows,distribute_prices(rows,base,market,undercut,discount)):
  checks={'positive':n.proposed_price>0,'floor':n.proposed_price>=floor_price(row.price,discount),'cap':n.proposed_price<=n.original_price,'cents':round(n.proposed_price,2)==n.proposed_price,'finite':math.isfinite(n.proposed_price)}
  nights+=1
  if not all(checks.values()):violations.append({'case':case,'base':base,'market':market,'discount':discount,'undercut':undercut,'night':n.model_dump(mode='json'),'failed':[k for k,v in checks.items() if not v]})
print(json.dumps({'schedules':count,'night_checks':nights,'violations':len(violations),'examples':violations[:5]},indent=2))
raise SystemExit(1 if violations else 0)
