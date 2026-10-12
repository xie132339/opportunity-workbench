# Offline only; run with PYTHONDONTWRITEBYTECODE=1 using the project virtualenv.
from pathlib import Path
from datetime import datetime,timezone,timedelta
import importlib.util,json,hashlib,sqlite3
r=Path('/Applications/work/gitRepo/opportunity-workbench'); out=Path('/private/tmp/opportunity-investigation-20261009'); out.mkdir(exist_ok=True)
p=r/'services/taobao_alliance.py'; spec=importlib.util.spec_from_file_location('probe_taobao',p); mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
now=datetime(2026,10,9,12,tzinfo=timezone(timedelta(hours=8)))
base={'num_iid':'100000000001','title':'离线测试抽纸100抽20包','item_url':'https://item.taobao.com/item.htm?id=100000000001','reserve_price':'10','zk_final_price':'10','coupon_amount':'2','coupon_start_fee':'10','coupon_start_time':'2026-10-09 11:00:00','coupon_end_time':'2026-10-09 13:00:00','coupon_remain_count':'20'}
cases=[('control',{}),('missing_reference',{'reserve_price':None}),('numeric_zero',{'zk_final_price':'8','coupon_start_fee':'8','coupon_remain_count':0}),('string_zero',{'zk_final_price':'8','coupon_start_fee':'8','coupon_remain_count':'0'}),('date_only',{'coupon_start_time':'2026-10-09','coupon_end_time':'2026-10-09'}),('exact_end',{'coupon_end_time':'2026-10-09 12:00:00'})]
results=[]
for name,delta in cases:
 row=mod.parse_item(dict(base,**delta),now=now)
 fields=row[5]['taobao_alliance_offer'] if row else {}
 results.append({'case':name,'input_delta':delta,'accepted':row is not None,'coupon_state':fields.get('coupon_state'),'quote_cents':fields.get('quote_price_cents'),'coupon_remain_count':fields.get('coupon_remain_count')})
info={'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mode':'synthetic offline; no requests; no database writes','frozen_now':now.isoformat(),'base_input':base,'results':results}
(out/'parser-probe.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)); print(json.dumps(info,ensure_ascii=False,indent=2))
