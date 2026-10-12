"""Periodic, source-attributed 2026 Double 11 rule and material snapshot."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from db import connect
from services.taobao_alliance import fetch_promoter_material_ids

RULE_URL = 'https://1111.alimama.com/alimamamain/page_20211227_144813'
ACTIVITIES = (
    ('天天开红包', '二、天天开红包规则', '每人每天最多开 1 次；领取后在“我的权益-红包”查看。', '每天可参与1次'),
    ('攒能量领红包', '三、攒能量值领红包规则', '在手机淘宝完成会场任务攒能量；达到门槛后主动领取，全周期最多 3 个。', '全周期最多领取3个超级红包'),
    ('逛会场领淘礼金', '四、逛会场领淘礼金规则', '在会场“为你推荐”领取指定商品淘礼金；领取后 24 小时内用于该商品。', '领取后24小时内可使用'),
    ('限时加码红包', '五、限时加码红包规则', '关注会场的限时红包弹窗；场次不固定，数量有限。', '场次安排不固定'),
    ('粉丝金币加抵', '四、粉丝专享金币加抵规则', '通过指定超级红包会场进入淘金币频道；有可用金币且商品符合范围时，查看实际加抵。', '金币加抵'),
    ('邀好友抢红包', '五、邀好友抢最高100元红包', '10月30日起在会场参与邀请任务；达成页面要求后主动领取，数量有限。', '红包需要主动领取'),
    ('惊喜红包', '六、惊喜红包规则', '关注会场不定期开放的抢红包模块；名额有限。', '数量有限'),
    ('红包猜猜猜', '七、红包猜猜猜规则', '在手机端猜幸运数字；具体场次和红包数量以会场显示为准。', '猜幸运数字'),
)


def parse_rule_page(html):
    page = BeautifulSoup(html, 'html.parser')
    title = page.title.get_text(' ', strip=True) if page.title else ''
    paragraphs = [' '.join(p.get_text(' ', strip=True).split()) for p in page.select('p')]
    body = ' '.join(paragraphs)
    if not re.search(r'(?:26年|2026年).{0,10}双11', title) or not all(
        value in body for value in ('2026年10月15日10:00:00', '2026年11月13日23:59:50',
                                     '优惠券、购物津贴、红包', '手机号', '实名认证')):
        raise ValueError('官方规则页结构或2026活动关键条件变化，需要重新核对')
    activities = []
    for name, heading, action, marker in ACTIVITIES:
        if heading not in body or marker not in body:
            raise ValueError(f'官方规则中的“{name}”条件变化，需要重新核对')
        activities.append({'name': name, 'action': action, 'heading': heading})
    return {'title': title, 'activities': activities,
            'start': '2026-10-15 10:00:00', 'end': '2026-11-13 23:59:50',
            'phase2_pending': '二阶段红包使用待后续更新' in body,
            'eligibility': '淘宝账号绑定手机号、支付宝完成实名认证，并使用最新版手机淘宝。',
            'stacking': '依次使用符合条件的优惠券、购物津贴、红包；红包仅抵商品货款，不能抵运费或服务费。'}


def current_year_material(material):
    name = str(material.get('material_name') or '')
    if not ('双11' in name or '双十一' in name):
        return False
    if not re.search(r'(?:2026|26年)', name):
        return False
    start = str(material.get('start_time') or '')
    end = str(material.get('end_time') or '')
    return bool(start and end and start[:4] <= '2026' <= end[:4])


def snapshot(db):
    row = db.execute("SELECT * FROM festival_official_snapshots WHERE festival_code='double_11' AND festival_year=2026").fetchone()
    if not row:
        return None
    result = dict(row)
    result['rules'] = json.loads(result['rules_json']) if result['rules_json'] else None
    stored = json.loads(result['materials_json']) if result['materials_json'] else []
    result['materials'] = stored.get('candidates', []) if isinstance(stored, dict) else stored
    result['products_total'] = stored.get('products_total') if isinstance(stored, dict) else None
    result['rights_total'] = stored.get('rights_total') if isinstance(stored, dict) else None
    return result


def refresh(*, due_only=False, now=None, get=None, fetch=None):
    """Fetch outside transaction. A failed endpoint keeps its last good snapshot."""
    now = now or datetime.now(timezone.utc)
    checked = now.strftime('%Y-%m-%d %H:%M:%S')
    with connect() as db:
        previous = snapshot(db)
    if due_only and previous and previous['last_attempt']:
        last = datetime.fromisoformat(previous['last_attempt']).replace(tzinfo=timezone.utc)
        minutes = 60 if not (previous['rules_error'] or previous['materials_error']) else 15
        if now - last < timedelta(minutes=minutes):
            return None
    rules, materials, material_counts, errors = None, None, None, {}
    try:
        response = (get or requests.get)(RULE_URL, timeout=15)
        response.raise_for_status()
        if len(response.content) > 500_000:
            raise ValueError('官方规则页内容过大')
        rules = parse_rule_page(response.text)
        rules['sha256'] = hashlib.sha256(response.content).hexdigest()
    except (requests.RequestException, ValueError) as exc:
        errors['rules'] = str(exc)[:160]
    try:
        fetch_materials = fetch or fetch_promoter_material_ids
        products = fetch_materials(material_type=1)
        rights = fetch_materials(material_type=2)
        material_counts = {'products_total': len(products), 'rights_total': len(rights)}
        materials = [dict(item, current_year=item.get('material_type', 1) == 1 and current_year_material(item))
                     for item in products + rights
                     if '双11' in item.get('material_name', '') or '双十一' in item.get('material_name', '')]
    except Exception as exc:
        errors['materials'] = str(exc)[:160]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO festival_official_snapshots(festival_code,festival_year) VALUES('double_11',2026)")
        if rules is not None:
            db.execute("UPDATE festival_official_snapshots SET rules_json=?,rules_synced_at=?,rules_error='' WHERE festival_code='double_11' AND festival_year=2026",
                       (json.dumps(rules, ensure_ascii=False), checked))
        elif 'rules' in errors:
            db.execute("UPDATE festival_official_snapshots SET rules_error=? WHERE festival_code='double_11' AND festival_year=2026", (errors['rules'],))
        if materials is not None:
            db.execute("UPDATE festival_official_snapshots SET materials_json=?,materials_synced_at=?,materials_error='' WHERE festival_code='double_11' AND festival_year=2026",
                       (json.dumps(dict(candidates=materials, **material_counts), ensure_ascii=False), checked))
        elif 'materials' in errors:
            db.execute("UPDATE festival_official_snapshots SET materials_error=? WHERE festival_code='double_11' AND festival_year=2026", (errors['materials'],))
        db.execute("UPDATE festival_official_snapshots SET last_attempt=? WHERE festival_code='double_11' AND festival_year=2026", (checked,))
    return {'rules': rules is not None, 'materials': len(materials) if materials is not None else None,
            'current_year_materials': sum(item['current_year'] for item in materials or []),
            'product_count': (material_counts or {}).get('products_total'),
            'rights_count': (material_counts or {}).get('rights_total'), 'errors': errors}
