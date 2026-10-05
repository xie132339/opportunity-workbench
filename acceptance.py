"""Reproducible 50-lead business acceptance, never an engineering-test score."""
import argparse,json,sqlite3,re
from pathlib import Path
from collections import Counter,defaultdict,deque
from datetime import datetime,timezone,timedelta
from urllib.parse import urlparse
from comparison import merchant_identity,product_key,comparison_index,assess_readiness
from autoreview import offer_summary,moment
from offer import resource_topic,TOPIC_LABELS
from link_resolution import enrich
from db import DB_PATH

QUERY='''SELECT o.*,e.snippet,e.metadata_json,e.published_at,e.last_seen_at,
 s.platform,s.enabled,s.status AS source_status,s.interval_minutes,s.last_success,
 a.state AS auto_state,a.advertised_cents,a.detail_json
 FROM opportunities o JOIN events e ON e.id=o.event_id JOIN sources s ON s.id=o.source_id
 LEFT JOIN auto_reviews a ON a.opportunity_id=o.id ORDER BY o.id DESC'''

def rows_from(path):
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute(QUERY)]

def brief_of(r):return offer_summary(r['title'],r['url'],r.get('snippet',''),metadata=r.get('metadata_json','{}'),detail_json=r.get('detail_json'))

def marketplace(r):
    b=brief_of(r);found=set()
    for link in b['activity_links']+[r['url']]:
        h=urlparse(link).hostname or ''
        for suffix,name in [('jd.com','京东'),('taobao.com','淘宝/天猫'),('tmall.com','淘宝/天猫'),('tb.cn','淘宝/天猫'),('yangkeduo.com','拼多多'),('pinduoduo.com','拼多多'),('suning.com','苏宁'),('vip.com','唯品会')]:
            if h==suffix or h.endswith('.'+suffix):found.add(name)
    if not found:
        names=set(re.findall('京东|天猫|淘宝|拼多多|苏宁|唯品会',b['platforms']))
        found={('淘宝/天猫' if n in ('淘宝','天猫') else n) for n in names}
    return next(iter(found)) if len(found)==1 else '购买平台未明确'

def freeze(rows,current_only=False,now=None):
    now=now or datetime.now(timezone.utc).replace(tzinfo=None)
    buckets=defaultdict(deque);seen=set();seen_urls=set()
    for r in rows:
        if r['url'] in seen_urls:continue
        seen_urls.add(r['url']);b=brief_of(r)
        # Product leads only; no coupon face values, lottery prizes or forum chatter.
        if b['kind']!='purchase':continue
        if current_only:
            published=moment(r.get('published_at'))
            if (r.get('auto_state') not in ('observed','conditional') or not r.get('enabled')
                or r.get('source_status')!='healthy' or not published
                or not timedelta(0)<=now-published<=timedelta(hours=2)):
                continue
        key=product_key(r['title'])
        if not key or key in seen:continue
        seen.add(key);market=marketplace(r);topic=resource_topic(r['title'])
        if topic=='other':topic=r.get('topic','other')
        buckets[(market,topic)].append(dict(first_id=r['id'],url=r['url'],title=r['title'],market=market,topic=topic))
    selected=[]
    while buckets and len(selected)<50:
        for key in sorted(list(buckets)):
            selected.append(buckets[key].popleft())
            if not buckets[key]:del buckets[key]
            if len(selected)==50:break
    method=('从当前两小时内、来源健康且已提取报价的商品结果取样；' if current_only else
            '按购买平台和商品主题轮询，每桶最新优先；缺资料、过期、冲突均保留失败；')
    return dict(frozen_at=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),method=method+'来源URL及去价标题去重；淘宝与天猫合并为同一购买生态。非随机样本，不能外推全市场成功率。',items=selected)

def evaluate(rows,cohort,cache,now=None):
    rows=[enrich(r,cache) for r in rows];index=comparison_index(rows,now=now);latest={}
    for r in rows:latest.setdefault(r['url'],r)
    items=[]
    for sample in cohort['items']:
        r=latest.get(sample['url']);fail=[]
        if r is None:
            items.append(dict(sample,passed=False,failures=['样本已缺失'],checks={}));continue
        b=brief_of(r);c=index[r['id']];assessment=assess_readiness(r,c,b);checks=assessment['checks']
        items.append(dict(sample,id=r['id'],identity_label=assessment['identity_label'],checks=checks,
                          passed=assessment['proven_better'],failures=assessment['failures'],
                          total_cents=b.get('total_cents'),quantity=b.get('quantity'),
                          plan_state=assessment['plan_state'],published_at=r.get('published_at'),comparison=c['message']))
    markets=Counter(i['market'] for i in items);topics=Counter(i['topic'] for i in items)
    totals=Counter(k for i in items for k,v in i['checks'].items() if v)
    coverage=len(items)==50 and len(set(markets)-{'购买平台未明确'})>=3 and len(set(topics)-{'other'})>=5
    passed=sum(i['passed'] for i in items)
    return dict(evaluated_at=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),frozen_at=cohort['frozen_at'],target=40,sample_size=len(items),passed=passed,
                search_ready=totals['search_ready'],comparable=totals['two_comparable_offers'],coverage_passed=coverage,
                overall_passed=coverage and passed>=40,market_counts=dict(markets),topic_counts={TOPIC_LABELS.get(k,k):v for k,v in topics.items()},checks=dict(totals),failures=dict(Counter(x for i in items for x in i['failures'])),items=items)

def report_md(r):
    lines=['# 50条公开来源报价比较验收',f"\n结论：{r['passed']}/{r['sample_size']} 条在样本同口径来源报价中最低或并列最低；目标40/50；平台/品类覆盖达标：{'是' if r['coverage_passed'] else '否'}。",f"\n冻结时间（UTC）：{r['frozen_at']}；计算时间（UTC）：{r['evaluated_at']}。",'\n固定样本不会用新通过条目替换失败条目。本文只评估冻结批次里的来源公开报价比较，不证明账号可买、到手价最低或存在净利润；不能外推全市场成功率。','\n购买平台：'+json.dumps(r['market_counts'],ensure_ascii=False),'\n商品类别：'+json.dumps(r['topic_counts'],ensure_ascii=False),'\n样本平台由原文/链接识别，仅表示线索涉及该平台，不代表一手数据已接通。主题沿用冻结时自动分类，会员关键词等存在误分类，未据此替换样本或增加通过数。', '\n单项完成数：'+json.dumps(r['checks'],ensure_ascii=False),'\n缺口计数（同条可有多项）：'+json.dumps(r['failures'],ensure_ascii=False),'\n条件算术吻合只代表按来源描述能算出该金额，不证明券可用。商品地址解析时间不作为价格验证时间。零笔个人购买记录不影响本验收。','\n|记录|购买平台|商品|商品金额/件数|结果与缺口|','|---|---|---|---|---|']
    for i in r['items']:
        oid=i.get('id',i['first_id']);price='未知' if i.get('total_cents') is None else f"{i['total_cents']/100:.2f}元"
        lines.append(f"|[#{oid}](http://127.0.0.1:5002/opportunities/{oid})|{i['market']}|{i['title'].replace('|','/')}|{price} / {i.get('quantity') or '未知'}件|{'；'.join(i['failures']) or '通过'}|")
    return '\n'.join(lines)+'\n'

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,default=DB_PATH);parser.add_argument('--cohort',type=Path,default=Path('data/acceptance-80-cohort.json'));parser.add_argument('--output',type=Path,default=Path('data/acceptance-80-current'));parser.add_argument('--without-cache',action='store_true');parser.add_argument('--refresh-cohort',action='store_true');args=parser.parse_args()
    rows=rows_from(args.snapshot)
    if args.cohort.exists() and not args.refresh_cohort:cohort=json.loads(args.cohort.read_text())
    else:
        cohort=freeze(rows,current_only=args.refresh_cohort);args.cohort.write_text(json.dumps(cohort,ensure_ascii=False,indent=2))
    cache={}
    if not args.without_cache:
        with sqlite3.connect('file:'+str(DB_PATH)+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row;cache={r['url']:dict(r) for r in c.execute('SELECT * FROM link_resolutions')}
    # Use a fixed snapshot-time window for before/after measurement, isolating the enrichment effect.
    now=datetime.strptime(cohort['frozen_at'],'%Y-%m-%d %H:%M:%S') if args.snapshot!=DB_PATH else None
    r=evaluate(rows,cohort,cache,now);args.output.with_suffix('.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));args.output.with_suffix('.md').write_text(report_md(r));print(json.dumps({k:v for k,v in r.items() if k!='items'},ensure_ascii=False,indent=2))
