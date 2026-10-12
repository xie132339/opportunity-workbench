"""Exact, bounded coupon selection for an explicitly supplied purchase basket.

No cart access or checkout claim. Amounts are integer cents; rules are evidence-
backed input, not inferred from marketing titles. Unknown overlap never stacks.
"""
from datetime import datetime, timedelta, timezone
from itertools import combinations
from urllib.parse import urlparse

from services.money import cents

CHINA = timezone(timedelta(hours=8))
STORE_RULE = 'https://developer.alibaba.com/support/announcementDetail.htm?id=25828'
PACKET_RULE = 'https://1111.alimama.com/alimamamain/page_20211227_144813'
KINDS = {'item_coupon': 0, 'shop_coupon': 1, 'shop_discount': 1,
         'platform_coupon': 2, 'red_packet': 3, 'official_path': 0}


def _text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name}不能为空且不能超过 {limit} 字')
    return value.strip()


def _amount(value, name):
    try:
        amount = cents(value, required=True)
    except ValueError as exc:
        raise ValueError(f"{name}：{exc}") from None
    if amount > 100000000:
        raise ValueError(f'{name}超过范围')
    return amount


def _integer(value, name, maximum):
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= maximum:
        raise ValueError(f'{name}须为 1 到 {maximum} 的整数')
    return int(value)


def _time(value):
    try:
        result = datetime.fromisoformat(str(value))
        return result.replace(tzinfo=CHINA) if result.tzinfo is None else result.astimezone(CHINA)
    except (ValueError, TypeError):
        raise ValueError('请填写完整的计算时间和优惠有效期') from None


def _source(value):
    value = _text(value, '规则来源', 1000)
    url = urlparse(value)
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
        raise ValueError('规则来源须为商品或规则页面的 HTTP(S) 链接')
    return value


def _allocate(balances, indices, amount):
    """Proportional allocation, largest remainder; preserve every cent."""
    total = sum(balances[i] for i in indices)
    if not amount or not total:
        return
    shares = {i: amount * balances[i] // total for i in indices}
    rest = amount - sum(shares.values())
    order = sorted(indices, key=lambda i: (-(amount * balances[i] % total), i))
    for i in order[:rest]:
        shares[i] += 1
    for i, share in shares.items():
        balances[i] -= share


def calculate_basket(payload):
    if not isinstance(payload, dict):
        raise ValueError('购买清单格式错误')
    items, rules = payload.get('items'), payload.get('rules', [])
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise ValueError('一次计算 1 到 8 种商品')
    if not isinstance(rules, list) or len(rules) > 12:
        raise ValueError('一次最多计算 12 条优惠，避免组合计算拖慢页面')
    now = _time(payload.get('at') or datetime.now(CHINA).isoformat())
    if not _time('2026-03-10T00:00') <= now < _time('2027-01-01T00:00'):
        raise ValueError('当前维护的是 2026 年 3 月 10 日起的规则，请选择该年度适用时间')
    ids, originals, groups = {}, [], {}
    normalized_items = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError('商品格式错误')
        iid = _text(item.get('id'), '商品编号', 80)
        if iid in ids:
            raise ValueError('商品编号重复，请合并数量')
        ids[iid] = index
        group = _text(item.get('shop'), '同店子订单组', 100)
        title = _text(item.get('title'), '商品名称')
        quantity = _integer(item.get('quantity'), '数量', 100)
        price = _amount(item.get('price'), '商品单价')
        if not price:
            raise ValueError('商品单价须大于零')
        # Blank shipping must never become free shipping.
        shipping = _amount(item.get('shipping'), '子订单运费')
        if group in groups and groups[group] != shipping:
            raise ValueError('同一子订单的运费须一致，运费按子订单收取一次')
        groups[group] = shipping
        originals.append(price * quantity)
        normalized_items.append({'id': iid, 'title': title, 'quantity': quantity,
                                 'shop': group, 'price_cents': price})
    rule_ids, active, ignored = set(), [], []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError('优惠规则格式错误')
        rid = _text(rule.get('id'), '优惠编号', 80)
        if rid in rule_ids:
            raise ValueError('优惠编号重复，同一张券只能录入一次')
        rule_ids.add(rid)
        name = _text(rule.get('name'), '优惠名称')
        kind = rule.get('kind')
        if kind not in KINDS:
            raise ValueError('不支持的优惠类型')
        scope = rule.get('items')
        if not isinstance(scope, list) or not scope or any(not isinstance(i, str) or i not in ids for i in scope):
            raise ValueError(f'{name}：请明确选择适用商品')
        indices = sorted({ids[i] for i in scope})
        if kind in {'shop_coupon', 'shop_discount'} and len({items[i]['shop'] for i in indices}) != 1:
            raise ValueError('店铺优惠只能用于同一子订单组')
        if kind in {'item_coupon', 'official_path'} and len(indices) != 1:
            raise ValueError('单品优惠只能选择一种商品')
        if kind == 'official_path' and normalized_items[indices[0]]['quantity'] != 1:
            ignored.append({'name': name, 'reason': '接口路径仅证明单件条件；改数量后不套用'})
            continue
        if rule.get('confirmed') is not True:
            ignored.append({'name': name, 'reason': '尚未确认账号资格、适用商品或当前价格是否已含该优惠'})
            continue
        source = _source(rule.get('source'))
        start, end = _time(rule.get('start')), _time(rule.get('end'))
        if start >= end:
            raise ValueError(f'{name}：结束时间须晚于开始时间')
        if not start <= now < end:
            ignored.append({'name': name, 'reason': '计算时间不在优惠有效期内'})
            continue
        if kind == 'red_packet' and not (_time('2026-10-15T10:00:00') <= now < _time('2026-11-13T23:59:50')):
            ignored.append({'name': name, 'reason': '不在已核实的 2026 双11超级红包活动期'})
            continue
        mode = rule.get('mode', 'minus')
        basis = rule.get('basis', 'current')
        if mode not in {'minus', 'repeat', 'percent'} or basis not in {'original', 'current'}:
            raise ValueError('优惠计算方式或门槛口径错误')
        threshold = _amount(rule.get('threshold', '0'), '优惠门槛')
        value = _amount(rule.get('value'), '优惠金额或折扣')
        if not value or (mode == 'percent' and value >= 10000):
            raise ValueError('优惠金额须大于零；折后百分比须大于 0 且小于 100')
        if mode == 'repeat' and not threshold:
            raise ValueError('每满减的门槛须大于零')
        if kind == 'red_packet' and (mode != 'minus' or basis != 'current'):
            raise ValueError('超级红包按券后商品金额判断门槛并按面额抵扣')
        cap = _amount(rule['cap'], '优惠封顶') if rule.get('cap') not in (None, '') else None
        active.append(dict(id=rid, name=name, kind=kind, indices=indices, source=source,
                           mode=mode, basis=basis, threshold=threshold, value=value, cap=cap,
                           min_quantity=_integer(rule.get('min_quantity', 1), '最低件数', 800)))
    pairs = payload.get('stack_pairs', [])
    if not isinstance(pairs, list) or len(pairs) > 66:
        raise ValueError('叠加关系格式错误')
    compatible = set()
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(r, str) or r not in rule_ids for r in pair) or pair[0] == pair[1]:
            raise ValueError('叠加关系引用了不存在的优惠')
        compatible.add(frozenset(pair))
    active.sort(key=lambda r: KINDS[r['kind']])  # Stable order within a level is explicit input order.
    conflicts = set()
    for a, b in combinations(range(len(active)), 2):
        left, right = active[a], active[b]
        overlap = bool(set(left['indices']) & set(right['indices']))
        shop_conflict = (left['kind'].startswith('shop_') and right['kind'].startswith('shop_')
                         and normalized_items[left['indices'][0]]['shop'] == normalized_items[right['indices'][0]]['shop'])
        path_conflict = 'official_path' in (left['kind'], right['kind']) and overlap
        packet_pair = 'red_packet' in (left['kind'], right['kind'])
        if shop_conflict or path_conflict or (overlap and not packet_pair and frozenset((left['id'], right['id'])) not in compatible):
            conflicts.add((a, b))
    results, applied_any = [], set()
    for mask in range(1 << len(active)):
        chosen = [i for i in range(len(active)) if mask & (1 << i)]
        if any((a, b) in conflicts for a, b in combinations(chosen, 2)):
            continue
        packets = [active[i] for i in chosen if active[i]['kind'] == 'red_packet']
        if len(packets) > 10 or sum(r['threshold'] > 0 for r in packets) > 1:
            continue
        balances, steps, feasible = originals.copy(), [], True
        for i in chosen:
            rule = active[i]
            scope = rule['indices']
            remaining = sum(balances[j] for j in scope)
            base = sum(originals[j] for j in scope) if rule['basis'] == 'original' else remaining
            quantity = sum(normalized_items[j]['quantity'] for j in scope)
            if base < rule['threshold'] or quantity < rule['min_quantity']:
                feasible = False
                break
            if rule['mode'] == 'percent':
                discount = remaining * (10000 - rule['value']) // 10000
            elif rule['mode'] == 'repeat':
                discount = (base // rule['threshold']) * rule['value']
            else:
                discount = rule['value']
            discount = min(discount, remaining)
            if rule['cap'] is not None:
                discount = min(discount, rule['cap'])
            if not discount:
                feasible = False
                break
            _allocate(balances, scope, discount)
            steps.append({'id': rule['id'], 'name': rule['name'], 'discount_cents': discount,
                          'goods_after_cents': sum(balances), 'source': rule['source']})
        if feasible:
            applied_any.update(s['id'] for s in steps)
            results.append({'total_cents': sum(balances) + sum(groups.values()), 'steps': steps,
                            'goods_cents': sum(balances), 'shipping_cents': sum(groups.values()),
                            'item_totals': balances})
    results.sort(key=lambda r: (r['total_cents'], len(r['steps']), tuple(s['id'] for s in r['steps'])))
    for rule in active:
        if rule['id'] not in applied_any:
            ignored.append({'name': rule['name'], 'reason': '适用商品的金额或件数未达门槛，或抵扣金额为零'})
    best = results[0]
    best['saving_cents'] = sum(originals) + sum(groups.values()) - best['total_cents']
    return {'best': best, 'alternatives': results[1:4], 'ignored': ignored,
            'items': normalized_items, 'evaluated': len(results), 'at': now.isoformat(),
            'example': payload.get('example') is True,
            'conflict_count': len(conflicts), 'base_cents': sum(originals) + sum(groups.values())}


def example_basket():
    """Illustrative prices/asset amounts; never persisted as real promotions."""
    source = STORE_RULE
    common = dict(items=['A'], start='2026-10-15T10:00', end='2026-10-20T19:59',
                  confirmed=True, source=source, basis='original', threshold='200', min_quantity=1)
    return {'example': True, 'at': '2026-10-16T12:00', 'items': [
        {'id': 'A', 'title': '示例商品（虚构金额）', 'shop': '示例店子订单1', 'price': '100', 'quantity': 2, 'shipping': '0'}],
        'rules': [dict(common, id='R1', name='店铺满200减10', kind='shop_coupon', mode='minus', value='10'),
                  dict(common, id='R2', name='同款2件9折', kind='shop_discount', mode='percent', value='90', min_quantity=2),
                  dict(common, id='R3', name='示例已领红包5元', kind='red_packet', mode='minus', value='5',
                       basis='current', threshold='0', source=PACKET_RULE)], 'stack_pairs': []}


def import_product(db, opportunity_id):
    """Read one collected item by primary key; no full scan or network call."""
    from services.taobao_alliance import validated_offer_for_row
    row = db.execute('''SELECT o.id,o.title,o.url,e.metadata_json,e.last_seen_at,
        s.method,s.enabled,s.last_success,s.interval_minutes FROM opportunities o
        JOIN events e ON e.id=o.event_id JOIN sources s ON s.id=o.source_id
        WHERE o.id=? AND o.status!='ignored' ''', (opportunity_id,)).fetchone()
    if not row or row['method'] != 'taobao_alliance' or not row['enabled']:
        raise ValueError('该编号不是已启用的淘宝联盟商品，请从商品详情地址查找编号')
    row = dict(row)
    freshness = datetime.now(CHINA) - timedelta(minutes=min(2 * row['interval_minutes'] + 15, 120))
    for key in ('last_seen_at', 'last_success'):
        try:
            moment = datetime.fromisoformat(row[key])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            row[key] = moment.astimezone(CHINA).isoformat()
            if moment < freshness:
                raise ValueError('商品采集已过期，请先同步来源再导入')
        except (TypeError, ValueError):
            raise ValueError('商品采集已过期或缺少时间，请先同步来源再导入') from None
    offer = validated_offer_for_row(row)
    if not offer:
        raise ValueError('当前官方报价字段不足，请同步来源后重试')
    iid = f"P{row['id']}"
    draft = {'item': {'id': iid, 'title': row['title'], 'price': f"{offer['promotion_price_cents']/100:.2f}",
                      'quantity': 1, 'shop': iid, 'shipping': '', 'source': row['url'],
                      'observed_at': row['last_seen_at']}, 'rules': []}
    if offer.get('coupon_amount_cents') and offer.get('coupon_threshold_cents') is not None and offer.get('coupon_state') in {'active_candidate', 'threshold_not_met'}:
        draft['rules'].append({'id': iid + '-coupon', 'name': '接口单品券（请核对是否同一张券）',
            'kind': 'item_coupon', 'items': [iid], 'threshold': f"{offer['coupon_threshold_cents']/100:.2f}",
            'value': f"{offer['coupon_amount_cents']/100:.2f}", 'mode': 'minus', 'basis': 'original',
            'min_quantity': 1, 'source': row['url'], 'confirmed': False,
            'start': str(offer.get('coupon_start_time') or '')[:19], 'end': str(offer.get('coupon_end_time') or '')[:19]})
    path = offer.get('promotion_path') or {}
    if path.get('complete'):
        parts = path['components']
        draft['rules'].append({'id': iid + '-path', 'name': '官方完整单件路径（整体择一）',
            'kind': 'official_path', 'items': [iid], 'threshold': '0',
            'value': f"{path['known_discount_cents']/100:.2f}", 'mode': 'minus', 'basis': 'current',
            'min_quantity': 1, 'source': row['url'], 'confirmed': False,
            'start': max(p['start_at'] for p in parts)[:19], 'end': min(p['end_at'] for p in parts)[:19]})
    return draft
