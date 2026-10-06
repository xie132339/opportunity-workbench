"""Admission of explicit public prices to the visible product-offer list."""


UNUSABLE_REVIEW_STATES = frozenset({
    'conflict', 'excluded', 'retry', 'source_unavailable', 'stale',
})
UNBOUND_PRICE_STATES = frozenset({
    'amount_basis_unconfirmed', 'multiple_amounts', 'source_amount_unbound',
    'title_amount_unbound', 'starting_price_unbound',
    'merchant_page_amount_unbound',
})


def quote_note(brief):
    """Use a non-price explanation when a source amount is not a usable quote."""
    state = brief.get('price_status', {}).get('state')
    if brief.get('net_after_rebate_cents') is not None and brief.get('total_cents') is None:
        return '来源只声称返现后的净成本；返现前付款金额与到账条件未核实，不作为购买报价或低价样本。'
    if state == 'merchant_page_amount_unbound':
        return brief.get('price_status', {}).get('note') or '商家页面金额是条件/预估线索，不作为报价。'
    if state in UNBOUND_PRICE_STATES:
        note = brief.get('price_status', {}).get('note')
        return ((note + ' ') if note else '原文含金额线索。') + '尚未形成可执行整单报价；不参与低价排序、预算筛选、同款比较或利润判断。'
    return brief.get('price_status', {}).get('note') or '当前证据不足，不能作为可用报价显示。'


def listing_quote(row, brief):
    """Return a price display only when its source and amount basis are explicit.

    Feed claims remain claims; catalogue observations remain public listing
    prices. Ambiguous or absent money stays in the clue inbox, never rendered as
    a purchasable offer or an invented zero.
    """
    if row.get('auto_state') in UNUSABLE_REVIEW_STATES:
        return None
    if row.get('catalog_listing'):
        amount = row.get('advertised_cents')
        if amount is None:
            return None
        return dict(amount_cents=amount, kind='catalog_public',
                    label='商城目录公开标价',
                    note='目录公开观察值；商品变体、库存、优惠和最终结算价未确认。')
    total = brief.get('total_cents')
    quantity = brief.get('quantity')
    if total is None or not quantity or brief.get('error'):
        return None
    return dict(amount_cents=total, kind='source_claim',
                label=f'来源原文整单声称价 · {quantity}件',
                note='金额与购买件数来自来源原文；不是商家结算价。')
