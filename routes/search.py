"""Search fresh source leads and optionally filter for comparison readiness."""
import json
from datetime import datetime, timezone
from flask import render_template, request
import db as database
from db import connect
from comparison import MAX_TARGET_IDS_FOR_PEER_EXPANSION, load_comparisons, assess_readiness
from autoreview import classify_catalog_observation, offer_summary
from benefits import linked_counts as linked_benefit_counts
from offer import RESOURCE_LABELS, TOPIC_LABELS, paper_package_prices, resource_topic
from link_resolution import enrich as enrich_links, resolution_cache
from services.money import cents
from services.opportunity_analysis import is_current_notice, is_evidence_candidate, notice_rows
from services.source_freshness import CATALOG_LISTING_PARSERS
from services.product_search import (listing_quote, quote_note,
                                     is_promotional_price_candidate, promotion_admission,
                                     official_promotion_discount, meets_minimum_official_discount,
                                     affiliate_promotion)

# 默认优惠收件箱的 Python 校验/排序候选池上限：SQL 层先按推广价预取这批机会，
# 再全量跑品牌判据与假优惠拦截。2G ECS 上全量（6 万行宽行）会 60s+ 超时，
# 收敛到该池后单页加载 <10s，展示行仍是池内精确排序的最优优惠。
OFFER_TOP_N = 1500
from services.source_provenance import annotate_search_sources, build_search_source_index
from services.search_diagnostics import summarize_search_funnel
from services.public_campaign import parse_public_campaign
from services.marketplace_identity import (
    FILTER_OPTIONS, attach_product_marketplace, marketplace_matches_filter,
)
from services.maintained_price_baselines import assess_maintained_baseline, load_baselines
from services.discount_calibration import assess_brand_baseline, topic_key
from services.taobao_alliance import validated_offer_for_row
from services.search_result_cache import (get as get_prepared_search, put as put_prepared_search,
                                           get_page as get_page_html, put_page as put_page_html)


def attach_offer_data(row, include_diagnostics=True):
    """Attach admission fields, deferring render-only diagnostics when safe."""
    row['display_topic']=resource_topic(row.get('title',''),row.get('source_category',''))
    row['catalog_listing']=row.get('source_parser') in CATALOG_LISTING_PARSERS
    row['campaign_window']=None
    if row.get('auto_state') in ('campaign_upcoming','campaign_active'):
        campaign_text='\n'.join(str(row.get(key) or '') for key in
                                 ('title','snippet','auto_conditions','detail_json'))
        row['campaign_window']=parse_public_campaign(campaign_text)
        if row['campaign_window']:
            row['campaign_start_at']=row['campaign_window']['start_at'].isoformat(sep=' ')
            row['campaign_end_at']=row['campaign_window']['end_at'].isoformat(sep=' ')
    taobao_offer = (validated_offer_for_row(row)
                    if row.get('source_method') == 'taobao_alliance' else None)
    brief=offer_summary(row['title'],row['url'],row.get('snippet'),row.get('auto_conditions'),
                        row.get('metadata_json'),row.get('detail_json'),
                        dict(checked_at=row.get('review_checked_at'),
                             detail_checked_at=row.get('detail_checked_at'),
                             detail_error=row.get('detail_error')),
                        taobao_offer=taobao_offer,
                        include_diagnostics=include_diagnostics)
    row['offer_brief']=brief
    row['_offer_diagnostics_deferred'] = bool(taobao_offer and not include_diagnostics)
    row['display_quote']=listing_quote(row,brief)
    row['catalog_reference']=bool(row['catalog_listing'] and row['display_quote']
                                  and row['display_quote'].get('kind')=='catalog_public')
    row['promotional_candidate']=is_promotional_price_candidate(row,brief)
    row['official_discount'] = official_promotion_discount(row)
    quote = row['display_quote']
    row['mobile_price_cents'] = quote['amount_cents'] if quote else None
    row['mobile_price_label'] = '价格'
    row['mobile_discount_cents'] = None
    row['mobile_discount_estimated'] = False
    row['mobile_discount_rate'] = None
    if (taobao_offer and quote and quote['kind'] == 'official_promotion_observation'
            and row['official_discount']):
        basis = row['official_discount']['basis']
        promotion = taobao_offer.get('promotion_price_cents')
        if basis == '推广价相对官方参考价差':
            before, after = taobao_offer.get('reserve_price_cents'), promotion
            row['mobile_discount_rate'] = row['official_discount'].get('rate_basis_points')
        elif basis == '单券条件试算减免':
            before, after = promotion, quote['amount_cents']
        elif basis == '完整官方促销路径减免':
            before = promotion
            after = (taobao_offer.get('promotion_path') or {}).get('estimate_cents')
        else:
            before = after = None
        if (isinstance(before, int) and isinstance(after, int)
                and before > after > 0):
            if row['mobile_discount_rate'] is None:
                row['mobile_discount_rate'] = (before - after) * 10000 // before
            row['mobile_price_cents'] = before
            row['mobile_price_label'] = ('参考价' if basis == '推广价相对官方参考价差'
                                         else '推广价')
            row['mobile_discount_cents'] = after
            row['mobile_discount_estimated'] = basis != '推广价相对官方参考价差'
    elif (taobao_offer and quote and quote['kind'] == 'source_claim'
          and quote['label'] == '淘宝联盟 SKU 预估优惠价'):
        detail = row.get('detail_json') or {}
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = {}
        if isinstance(detail, dict) and detail.get('field_state') == 'bound':
            estimate = detail.get('promotion_estimate_cents')
            before = detail.get('public_sale_cents') if estimate else detail.get('reference_cents')
            after = quote['amount_cents']
            if isinstance(before, int) and before > after > 0:
                row['mobile_price_cents'] = before
                row['mobile_price_label'] = 'SKU标价' if estimate else 'SKU参考价'
                row['mobile_discount_cents'] = after
                row['mobile_discount_estimated'] = bool(estimate)
    row['affiliate_promotion'] = affiliate_promotion(row)
    row['display_quote_note']=quote_note(brief)
    return row


def can_skip_alliance_link_enrichment(row):
    """True only when Alliance fields themselves establish marketplace type."""
    if row.get('source_method') != 'taobao_alliance':
        return False
    offer = validated_offer_for_row(row)
    return bool(offer and offer.get('link_kind') == 'official_promotion_link'
                and offer.get('marketplace_key') in {'taobao', 'tmall'})


def attach_search_marketplace(row, link_cache):
    """Attach marketplace identity without resolving Alliance promotion links.

    Only official promotion links carry the merchant type and product identity
    needed by marketplace identity. Resolving those URLs again does not add a
    default-list admission signal; direct merchant URLs still use the normal
    resolver path.
    """
    if can_skip_alliance_link_enrichment(row):
        return attach_product_marketplace(row)
    return attach_product_marketplace(enrich_links(row, link_cache))


def prepare_standard_results(db, *, sql, args, order_by, topic, buy_platform,
                             show_archive, ready_only, priced_only, budget,
                             minimum_discount_cents, sort_mode, maintained_baselines, funnel_scope_sql,
                             funnel_scope_args):
    """Prepare the complete result set once before page-specific rendering.

    Promotion admission and comparison ordering apply to the whole matching
    corpus.  Keeping this work here makes it explicit that page slicing is a
    rendering concern, and lets the caller reuse this prepared result safely.
    """
    # 默认优惠收件箱（offers 视图）两阶段预取：先以轻量列（id+推广价）在 SQL 层
    # 去重并取推广价最高的前 OFFER_TOP_N 个机会，再回查这些 id 的完整宽行。
    # 避免 6 万行宽行全量物化 + 大排序把 2G ECS 拖到 60s+（nginx 超时→504"访问不通"）。
    # 语义：排序仍由下方 Python 精确完成（含品牌判据/假优惠拦截），只是候选池
    # 收敛为推广价最高的一批——优惠额大的真优惠不会因此漏掉。
    if (priced_only and buy_platform == 'taobao' and not show_archive
            and not ready_only):
        scope = sql.split(" FROM ", 1)[1]
        id_sql = ("SELECT o.id, MAX(COALESCE(a.advertised_cents,0)) AS adv FROM "
                  + scope + " GROUP BY o.id")
        top = sorted((dict(row) for row in db.execute(id_sql, args)),
                     key=lambda row: (row["adv"], row["id"]), reverse=True)[:OFFER_TOP_N]
        top_ids = [row["id"] for row in top]
        if top_ids:
            sql = sql + " AND o.id IN (" + ",".join("?" for _ in top_ids) + ")"
            args = list(args) + top_ids
    raw_rows = [dict(row) for row in db.execute(sql + order_by, args)]
    # 展示层折叠：淘宝联盟同款（同归一化标题、不同店铺/多轮快照）只保留排序最优一条。
    # SQL 已按 order_by 排序，每组第一个即最优/最新；非淘宝来源（external_key 唯一）不折叠。
    if raw_rows:
        seen_keys = set()
        folded = []
        for row in raw_rows:
            key = row.get('id')
            if row.get('source_method') == 'taobao_alliance':
                ek = row.get('event_external_key') or ''
                if ek.startswith('taobao:'):
                    rest = ek[len('taobao:'):]
                    key = ('t', rest.split('|')[0])
            else:
                key = ('o', key)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            folded.append(row)
        raw_rows = folded
    rows_needing_link_enrichment = [
        row for row in raw_rows if not can_skip_alliance_link_enrichment(row)
    ]
    link_cache = resolution_cache(db, rows_needing_link_enrichment)
    all_rows = [attach_search_marketplace(row, link_cache) for row in raw_rows]
    for row in all_rows:
        row['display_topic'] = resource_topic(row.get('title', ''), row.get('source_category', ''))
    if topic in TOPIC_LABELS:
        all_rows = [row for row in all_rows if row['display_topic'] == topic]
    marketplace_population_count = len(all_rows)
    marketplace_unidentified_count = sum(row['marketplace_key'] == 'unknown' for row in all_rows)
    marketplace_selected_count = sum(
        marketplace_matches_filter(row['marketplace_key'], buy_platform)
        for row in all_rows
    )
    if buy_platform != 'all':
        all_rows = [row for row in all_rows if marketplace_matches_filter(row['marketplace_key'], buy_platform)]
    if not show_archive:
        # A live catalogue row may have an old saved ``missing_time`` result
        # from the former post-only rule. Re-evaluate that row for display
        # against its current successful snapshot; do not write it here.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for row in all_rows:
            row['catalog_listing'] = row.get('source_parser') in CATALOG_LISTING_PARSERS
            last_seen = row.get('last_seen_at') or ''
            checked = row.get('review_checked_at') or ''
            review_behind = bool(last_seen and checked and checked < last_seen)
            if row['catalog_listing'] and (not row.get('auto_state') or row.get('auto_state') == 'missing_time' or review_behind):
                result = classify_catalog_observation(row, now)
                row['auto_state'] = result['state']
                row['auto_reason'] = result['reason']
                row['advertised_cents'] = result['advertised_cents']
    else:
        for row in all_rows:
            row['catalog_listing'] = row.get('source_parser') in CATALOG_LISTING_PARSERS
    source_comparison_deferred = not ready_only and len(all_rows) > MAX_TARGET_IDS_FOR_PEER_EXPANSION
    for row in all_rows:
        attach_offer_data(row, include_diagnostics=not source_comparison_deferred)
        maintained = assess_maintained_baseline(
            row, row['offer_brief'], row.get('display_quote'), maintained_baselines)
        # 人工维护基准优先；未命中时回退品牌标准市场价自动基准（95 品牌配置表，
        # 即用户要求全面维护的配置数据），使 AC-05 维护基准判断实际生效。
        if maintained.get('state') in ('no_match', 'unit_unavailable', 'not_official_promotion'):
            brand = assess_brand_baseline(
                topic_key(row.get('source_name')) or row.get('display_topic') or row.get('topic'),
                row.get('title') or (row.get('offer_brief') or {}).get('title') or '',
                (row.get('display_quote') or {}).get('amount_cents'))
            if brand.get('state') != 'no_match':
                maintained = brand
        row['maintained_baseline'] = maintained
    source_match_counts = {}
    source_quote_counts = {}
    for matched_row in all_rows:
        source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
        if matched_row.get('display_quote'):
            source_quote_counts[matched_row['source_id']] = source_quote_counts.get(matched_row['source_id'], 0) + 1
    source_offer_count = candidate_pool_count = len(all_rows)
    comparisons = ({} if source_comparison_deferred else
                   (load_comparisons(db) if show_archive else load_comparisons(db, [row['id'] for row in all_rows])))
    for row in all_rows:
        row['catalog_listing'] = row.get('source_parser') in CATALOG_LISTING_PARSERS
    search_assessments = {
        row['id']: assess_readiness(row, comparisons.get(row['id'], {}), row['offer_brief'])
        for row in all_rows
    }
    for row in all_rows:
        row['promotion_admission'] = promotion_admission(row, search_assessments.get(row['id']))
        row['public_offer_eligible'] = row['promotion_admission']['eligible']
    quality_counts = dict(
        search_ready=sum(bool(value['search_ready']) for value in search_assessments.values()),
        comparable=sum(bool(value['comparable']) for value in search_assessments.values()),
        source_claim_low=sum(bool(value['source_claim_low']) for value in search_assessments.values()),
        displayable_quote=sum(bool(row['display_quote']) for row in all_rows),
        promotional_candidate=sum(bool(row.get('promotional_candidate')) for row in all_rows),
        public_offer_eligible=sum(bool(row.get('public_offer_eligible')) for row in all_rows),
        minimum_discount_eligible=sum(
            bool(row.get('public_offer_eligible'))
            and meets_minimum_official_discount(row, minimum_discount_cents)
            for row in all_rows
        ),
        catalog_reference=sum(bool(row.get('catalog_reference')) for row in all_rows),
        plain_price_reference=sum(bool(row.get('display_quote')) and not row.get('promotional_candidate')
                                  and not row.get('catalog_reference') for row in all_rows),
        unpriced=sum(not row.get('display_quote') for row in all_rows),
    )
    quality_counts['excluded'] = candidate_pool_count - quality_counts['search_ready']
    unpriced_current_count = quality_counts['unpriced']
    search_funnel = None
    if funnel_scope_sql:
        funnel_scope_rows = [dict(row) for row in db.execute(funnel_scope_sql, funnel_scope_args)]
        search_funnel = summarize_search_funnel(funnel_scope_rows, all_rows)
    provenance_corpus = all_rows
    provenance_index = build_search_source_index(provenance_corpus)
    if ready_only and not show_archive:
        all_rows = [row for row in all_rows if search_assessments[row['id']]['search_ready']]
        source_match_counts = {}
        source_quote_counts = {}
        for matched_row in all_rows:
            source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            if matched_row['display_quote']:
                source_quote_counts[matched_row['source_id']] = source_quote_counts.get(matched_row['source_id'], 0) + 1
    elif priced_only:
        all_rows = [row for row in all_rows
                    if row.get('public_offer_eligible')
                    and meets_minimum_official_discount(row, minimum_discount_cents)]
    if budget is not None:
        all_rows = [row for row in all_rows if row.get('display_quote') and row['display_quote'].get('kind') in {
            'source_claim', 'official_promotion_observation'} and row['display_quote']['amount_cents'] <= budget]
        source_match_counts = {}
        source_quote_counts = {}
        for matched_row in all_rows:
            source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            source_quote_counts[matched_row['source_id']] = source_quote_counts.get(matched_row['source_id'], 0) + 1
    if sort_mode in ('discount_high', 'discount_rate_high'):
        def official_discount_key(row):
            discount = row.get('official_discount') or {}
            amount = discount.get('amount_cents')
            rate = discount.get('rate_basis_points')
            valid = isinstance(amount, int) and not isinstance(amount, bool) and amount > 0
            return (valid, amount if valid else 0,
                    rate if isinstance(rate, int) and not isinstance(rate, bool) else -1,
                    row.get('published_at') or row.get('last_seen_at') or '', row['id'])
        if sort_mode == 'discount_high':
            all_rows.sort(key=lambda row: (
                official_discount_key(row)[0], official_discount_key(row)[1],
                official_discount_key(row)[2], official_discount_key(row)[3], official_discount_key(row)[4]),
                reverse=True)
        else:
            all_rows.sort(key=lambda row: (
                official_discount_key(row)[0], official_discount_key(row)[2],
                official_discount_key(row)[1], official_discount_key(row)[3], official_discount_key(row)[4]),
                reverse=True)
    elif sort_mode == 'comparison':
        all_rows.sort(key=lambda row: (row.get('maintained_baseline', {}).get('rank', 0),
                                       comparisons.get(row['id'], {}).get('rank', 0), row['id']), reverse=True)
    elif sort_mode == 'latest':
        all_rows.sort(key=lambda row: (row.get('published_at') or row.get('last_seen_at') or '', row['id']), reverse=True)
    elif sort_mode in ('price_low', 'price_high', 'paper_unit_low',
                       'commission_low', 'commission_high'):
        priced, unpriced = [], []
        for row in all_rows:
            if sort_mode in ('commission_low', 'commission_high'):
                promotion = row.get('affiliate_promotion')
                value = (promotion.get('commission_amount_cents')
                         if isinstance(promotion, dict) else None)
            else:
                brief = row['offer_brief']
                quote = row.get('display_quote')
                value = quote.get('amount_cents') if quote else None
                if sort_mode == 'paper_unit_low':
                    selected_spec = brief.get('selected_spec')
                    paper = paper_package_prices(selected_spec, brief.get('total_cents'), brief.get('quantity')) if quote and quote['kind'] == 'source_claim' and selected_spec else None
                    value = paper.get('per_pack_cents') if paper else None
            if value is None:
                unpriced.append(row)
            else:
                row['_sort_price_cents'] = value
                priced.append(row)
        priced.sort(key=lambda row: (row['_sort_price_cents'], row.get('published_at') or row.get('last_seen_at') or '', row['id']),
                    reverse=sort_mode in ('price_high', 'commission_high'))
        for row in priced:
            row.pop('_sort_price_cents', None)
        all_rows = priced + unpriced
    return dict(
        all_rows=all_rows, comparisons=comparisons, search_assessments=search_assessments,
        quality_counts=quality_counts, source_match_counts=source_match_counts,
        source_quote_counts=source_quote_counts, source_offer_count=source_offer_count,
        candidate_pool_count=candidate_pool_count, source_comparison_deferred=source_comparison_deferred,
        unpriced_current_count=unpriced_current_count, search_funnel=search_funnel,
        provenance_corpus=provenance_corpus, provenance_index=provenance_index,
        marketplace_population_count=marketplace_population_count,
        marketplace_unidentified_count=marketplace_unidentified_count,
        marketplace_selected_count=marketplace_selected_count, total=len(all_rows),
    )


def index():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    platform = request.args.get("platform", "").strip()
    buy_platform = request.args.get("buy_platform", "taobao").strip().lower()
    valid_marketplaces = {key for key, _ in FILTER_OPTIONS}
    if buy_platform not in valid_marketplaces:
        buy_platform = "taobao"
    buy_platform_label = dict(FILTER_OPTIONS).get(buy_platform, "淘宝系官方商品")
    offer_filter = request.args.get("offer", "").strip()
    topic = request.args.get('topic','')
    resource = request.args.get('resource','')
    budget_text = request.args.get("budget", "").strip()
    minimum_discount_text = request.args.get("minimum_discount", "").strip()
    requested_sort = request.args.get("sort", "").strip()
    layout_mode = request.args.get("layout", "list")
    if layout_mode not in ("list", "cards"):
        layout_mode = "list"
    page_size = 100 if layout_mode == "list" else 36
    candidate_only = request.args.get("candidate") == "1"
    # The default product-search page is a promotional-price lead list. Plain
    # merchant catalogue prices remain reference evidence in the clue pool;
    # they are not presented as bargains just because a number was extracted.
    view_mode = request.args.get("view") or "offers"
    if view_mode not in ("offers", "ready", "current", "all"):
        view_mode = "offers"
    sort_mode = requested_sort or ("discount_high" if view_mode == "offers" else "comparison")
    if sort_mode not in ("comparison", "latest", "price_low", "price_high", "paper_unit_low",
                         "commission_low", "commission_high", "discount_high", "discount_rate_high"):
        sort_mode = "discount_high" if view_mode == "offers" else "comparison"
    show_archive = view_mode == "all"
    ready_only = view_mode == "ready"
    priced_only = view_mode == "offers"
    try:
        budget = cents(budget_text) if budget_text else None
    except ValueError:
        budget = None
    # 整页缓存：仅默认 offers 收件箱视图启用。同一参数的请求在 TTL(60s) 内
    # 直接返回已渲染 HTML，完全跳过 SQLite 查询与 Jinja 渲染。worker 全量 sync
    # 期间 2G 机器 CPU 满载，实时渲染会被拖到 27-58s；页面缓存保证首页/搜索
    # 永远秒回（最多 60s 旧）。candidate/ready/current/all 属审计视图，数据变化
    # 敏感且访问量低，不做页面缓存，保持所见即最新。
    page_cache_key = None
    if priced_only and not candidate_only and not ready_only and not show_archive:
        page_cache_key = ("index-html-v1", str(database.DB_PATH),
                          request.query_string.decode("utf-8", "replace"))
        cached_page = get_page_html(page_cache_key)
        if cached_page is not None:
            return cached_page
    try:
        minimum_discount_cents = cents(minimum_discount_text) if minimum_discount_text else 500
    except ValueError:
        minimum_discount_cents = 500
    minimum_discount_text = str(minimum_discount_cents / 100).rstrip('0').rstrip('.')
    sql = """SELECT o.*,s.platform,s.category AS source_category,s.status AS source_status,s.method AS source_method,s.parser AS source_parser,
             s.enabled AS source_enabled,s.interval_minutes AS source_interval,s.name AS source_name,
             s.last_success AS source_last_success,
             e.is_baseline,e.published_at,e.last_seen_at,e.url AS event_url,e.snippet,e.metadata_json,
             e.external_key AS event_external_key,
             a.state AS auto_state,a.reason AS auto_reason,a.advertised_cents,
             a.specification AS auto_specification,a.conditions AS auto_conditions,a.detail_json,
             a.checked_at AS review_checked_at,a.detail_checked_at,a.detail_error
             FROM opportunities o LEFT JOIN sources s ON s.id=o.source_id
             LEFT JOIN events e ON e.id=o.event_id
             LEFT JOIN auto_reviews a ON a.opportunity_id=o.id WHERE 1=1"""
    args = []
    # Stored topics are a cached classification and older rows may predate
    # current title rules. Filter against the same live classifier used for
    # display, so a stale saved “other” label cannot hide a current product.
    if resource in RESOURCE_LABELS:
        sql += ' AND o.resource_kind=?'
        args.append(resource)
    if query:
        # A single character is noisy in RSS copy. Allow source-copy-only hits
        # only when the same captured text contains a price marker; the title
        # remains searchable without that extra requirement.
        if len(query) == 1:
            # 联盟渠道标题即商品名，来源文本(events.snippet)是商品描述的大文本。
            # 对 snippet 做前导通配 LIKE 在 JOIN 后要全扫 6.2 万行大文本（30s+，
            # 曾把搜索页拖到 75s 触发 504"访问不通"）。联盟标题匹配已覆盖绝大多数
            # 相关商品，故仅对非联盟渠道保留来源文本匹配（RSS 等多平台场景需要）。
            sql += (" AND (o.title LIKE ? OR (s.method!='taobao_alliance' AND "
                    "COALESCE(e.snippet,'') LIKE ? AND (COALESCE(e.snippet,'') LIKE '%元%' "
                    "OR COALESCE(e.snippet,'') LIKE '%¥%' OR COALESCE(e.snippet,'') LIKE '%￥%')))")
            args.extend(("%" + query + "%", "%" + query + "%"))
            if query == '纸':
                # Common non-paper compound terms in deal feeds.
                for unrelated_title_term in ('纸皮', '响纸'):
                    sql += " AND o.title NOT LIKE ?"
                    args.append('%' + unrelated_title_term + '%')
        else:
            # 多字符搜索同样避免联盟渠道全扫 snippet 大文本：联盟标题已含关键词。
            sql += " AND (o.title LIKE ? OR (s.method!='taobao_alliance' AND COALESCE(e.snippet,'') LIKE ?))"
            args.extend(("%" + query + "%", "%" + query + "%"))
    if category:
        sql += " AND o.category=?"
        args.append(category)
    if platform:
        sql += " AND s.platform=?"
        args.append(platform)
    if offer_filter == "new_user":
        sql += " AND o.offer_type IN ('suspected_new_user','new_user')"
    elif offer_filter in ("standard", "other_restricted", "unknown"):
        sql += " AND o.offer_type=?"
        args.append(offer_filter)
    if status:
        sql += " AND o.status=?"
        args.append(status)
    else:
        sql += " AND o.status!='ignored'"
    if not show_archive:
        # Paused sources remain available in the explicit archive only. Their old
        # opportunities must not look like current products or influence this
        # search's funnel; manually entered records retain their own lifecycle.
        sql += " AND (s.id IS NULL OR s.enabled=1 OR s.method='manual')"
    if not show_archive:
        if not candidate_only:
            # Product search and the current lead inbox start from fresh purchase-like
            # records. The explicit ready view applies the shared evidence gate below.
            sql += " AND o.resource_kind IN ('purchase','unknown')"
    # The current clue pool still explains freshness exclusions, but needs no
    # resolver or offer parsing for rows that will not be displayed.  Keep its
    # lightweight SQL scope separate from the default official-offer inbox.
    funnel_scope_sql = None
    funnel_scope_args = ()
    if (query and view_mode == "current" and not show_archive and not candidate_only
            and not budget_text):
        funnel_scope_sql = sql
        funnel_scope_args = tuple(args)
    if not show_archive:
        sql += " AND COALESCE(a.state,'queued') NOT IN ('stale','source_unavailable','retry','excluded')"
        sql += """ AND ((s.method='manual' AND o.status='verified'
                         AND datetime(o.buy_checked_at) BETWEEN datetime('now','-15 minutes') AND CURRENT_TIMESTAMP)
                     OR (s.method!='manual' AND s.enabled=1 AND s.status='healthy'
                         AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                         AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                         AND (e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
                              OR a.state IN ('campaign_upcoming','campaign_active')
                              OR ((s.parser IN ('apple','mi','suning','lenovo','honor','kongfz')
                                   OR s.method='taobao_alliance')
                                  AND s.last_success BETWEEN datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes') AND CURRENT_TIMESTAMP))) )"""
    # The default offer view is explicitly the official Taobao Alliance
    # promotion inbox.  Constrain it before materialising rows so unrelated
    # source records are never link-resolved merely to be filtered later.
    if priced_only and buy_platform == 'taobao':
        sql += " AND s.method='taobao_alliance'"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    with connect() as db:
        if priced_only and buy_platform == 'taobao':
            # Start from the small set of currently healthy Alliance sources.
            # Without this explicit source-id scope SQLite may scan every
            # historical opportunity to satisfy a leading-wildcard title
            # search, even though the default inbox only accepts Alliance data.
            source_ids = [row['id'] for row in db.execute("""SELECT id FROM sources
                WHERE method='taobao_alliance' AND enabled=1 AND status='healthy'
                  AND last_success>=datetime('now','-' || MIN(2*interval_minutes+15,120) || ' minutes')""")]
            if source_ids:
                sql += " AND o.source_id IN (" + ",".join("?" for _ in source_ids) + ")"
                args.extend(source_ids)
            else:
                sql += " AND 0"
        maintained_baselines = load_baselines(db)
        search_funnel = None
        source_comparison_deferred = False
        order_by = """ ORDER BY CASE o.category
            WHEN '零售优惠' THEN 1 WHEN '二手与闲置' THEN 2
            WHEN '供货与清仓' THEN 3 WHEN '新品与补货' THEN 4
            WHEN '拍卖与资产' THEN 5 WHEN '服务与合作' THEN 6 ELSE 7 END,
            o.created_at DESC,o.id DESC"""
        if candidate_only:
            rules = db.execute("SELECT * FROM strategies WHERE enabled=1").fetchall()
            candidates = []
            raw_candidates = [dict(row) for row in db.execute(sql + " AND o.status='verified'" + order_by, args)]
            candidate_link_cache = resolution_cache(db, raw_candidates)
            candidate_rows = [attach_product_marketplace(enrich_links(row, candidate_link_cache))
                              for row in raw_candidates]
            marketplace_population_count = len(candidate_rows)
            marketplace_unidentified_count = sum(row['marketplace_key'] == 'unknown' for row in candidate_rows)
            marketplace_selected_count = sum(
                marketplace_matches_filter(row['marketplace_key'], buy_platform)
                for row in candidate_rows
            )
            if buy_platform != 'all':
                candidate_rows = [row for row in candidate_rows
                                  if marketplace_matches_filter(row['marketplace_key'], buy_platform)]
            for row in candidate_rows:
                if topic in TOPIC_LABELS and resource_topic(row['title'],row['source_category'] or '') != topic:
                    continue
                quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (row["id"],)).fetchall()
                if is_evidence_candidate(row, quotes, rules):
                    candidates.append(row)
            total = len(candidates)
            source_match_counts = {}
            source_quote_counts = {}
            prepared_candidates=[]
            for matched_row in candidates:
                prepared=attach_offer_data(dict(matched_row))
                if budget is not None and (not prepared['display_quote'] or prepared['display_quote']['amount_cents']>budget):
                    continue
                source_match_counts[prepared['source_id']] = source_match_counts.get(prepared['source_id'], 0) + 1
                if prepared['display_quote']:
                    source_quote_counts[prepared['source_id']]=source_quote_counts.get(prepared['source_id'],0)+1
                prepared_candidates.append(prepared)
            for prepared in prepared_candidates:
                maintained = assess_maintained_baseline(
                    prepared, prepared['offer_brief'], prepared.get('display_quote'), maintained_baselines)
                if maintained.get('state') in ('no_match', 'unit_unavailable', 'not_official_promotion'):
                    brand = assess_brand_baseline(
                        topic_key(prepared.get('source_name')) or prepared.get('display_topic') or prepared.get('topic'),
                        prepared.get('title') or (prepared.get('offer_brief') or {}).get('title') or '',
                        (prepared.get('display_quote') or {}).get('amount_cents'))
                    if brand.get('state') != 'no_match':
                        maintained = brand
                prepared['maintained_baseline'] = maintained
            total=len(prepared_candidates)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            page_buttons = list(range(1, min(total_pages, 10) + 1))
            rows = prepared_candidates[(page - 1) * page_size:page * page_size]
            search_assessments={}
            quality_counts=dict(search_ready=0,comparable=0,source_claim_low=0,excluded=0,
                                displayable_quote=sum(bool(row.get('display_quote')) for row in prepared_candidates),
                                promotional_candidate=sum(bool(row.get('promotional_candidate')) for row in prepared_candidates),
                                minimum_discount_eligible=sum(
                                    meets_minimum_official_discount(row, minimum_discount_cents)
                                    for row in prepared_candidates),
                                catalog_reference=sum(bool(row.get('catalog_reference')) for row in prepared_candidates),
                                plain_price_reference=sum(bool(row.get('display_quote')) and not row.get('promotional_candidate')
                                                          and not row.get('catalog_reference') for row in prepared_candidates),
                                unpriced=sum(not row.get('display_quote') for row in prepared_candidates))
            unpriced_current_count=quality_counts['unpriced']
            candidate_pool_count=total
            source_offer_count=total
            comparisons=load_comparisons(db,[row['id'] for row in rows])
        else:
            # Page and layout are intentionally absent: admission, comparison
            # and ordering are independent of the page being rendered. A write
            # in this process clears the cache at transaction commit; writes
            # from the separate collector process are bounded by its short TTL.
            # Use DB_PATH rather than SQLite's file label: isolated test
            # databases can share an internal label while still being distinct.
            database_file = str(database.DB_PATH)
            prepared_key = (
                'standard-search-v4', database_file,
                query, category, status, platform,
                buy_platform, offer_filter, topic, resource, budget, minimum_discount_cents, sort_mode,
                view_mode,
            )
            prepared = get_prepared_search(prepared_key)
            if prepared is None:
                prepared = prepare_standard_results(
                    db, sql=sql, args=args, order_by=order_by, topic=topic,
                    buy_platform=buy_platform, show_archive=show_archive,
                    ready_only=ready_only, priced_only=priced_only, budget=budget,
                    minimum_discount_cents=minimum_discount_cents,
                    sort_mode=sort_mode, maintained_baselines=maintained_baselines,
                    funnel_scope_sql=funnel_scope_sql, funnel_scope_args=funnel_scope_args,
                )
                put_prepared_search(prepared_key, prepared)
            # Rows are copied only for the page renderer below.  The cache holds
            # the prepared corpus unchanged until its short TTL expires.
            all_rows = list(prepared['all_rows'])
            comparisons = prepared['comparisons']
            search_assessments = dict(prepared['search_assessments'])
            quality_counts = dict(prepared['quality_counts'])
            source_match_counts = dict(prepared['source_match_counts'])
            source_quote_counts = dict(prepared['source_quote_counts'])
            source_offer_count = prepared['source_offer_count']
            candidate_pool_count = prepared['candidate_pool_count']
            source_comparison_deferred = prepared['source_comparison_deferred']
            unpriced_current_count = prepared['unpriced_current_count']
            search_funnel = prepared['search_funnel']
            provenance_corpus = prepared['provenance_corpus']
            provenance_index = prepared['provenance_index']
            marketplace_population_count = prepared['marketplace_population_count']
            marketplace_unidentified_count = prepared['marketplace_unidentified_count']
            marketplace_selected_count = prepared['marketplace_selected_count']
            total = prepared['total']
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            page_buttons = list(range(1, min(total_pages, 10) + 1))
            rows=all_rows[(page-1)*page_size:page*page_size]
            rows=annotate_search_sources(rows, provenance_corpus, provenance_index)
            rows_needing_link_enrichment = [
                row for row in rows if not can_skip_alliance_link_enrichment(row)
            ]
            product_link_cache=resolution_cache(db,rows_needing_link_enrichment)
            rows=[enrich_links(row,product_link_cache)
                  if not can_skip_alliance_link_enrichment(row) else row for row in rows]
            # Hydrate only records that the user can see.  This restores the
            # exact audit, promotion and sync text required by both list and
            # card templates without paying that parsing cost for every row.
            for row in rows:
                if row.pop('_offer_diagnostics_deferred', False):
                    attach_offer_data(row)
                    search_assessments[row['id']] = assess_readiness(
                        row, comparisons.get(row['id'], {}), row['offer_brief'])
        dedicated_taobao_source_count = db.execute(
            "SELECT COUNT(*) FROM sources WHERE enabled=1 AND (lower(parser)='taobao' OR method='taobao_alliance')"
        ).fetchone()[0]
        all_sources = [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY id").fetchall()]
        # Current search offers only active sources; the explicit archive keeps the
        # full catalog so paused-source history remains discoverable.
        sources = all_sources if show_archive else [
            source for source in all_sources if source['enabled'] or source['method'] == 'manual'
        ]
        fresh_events={r['source_id']:r['count'] for r in db.execute("SELECT source_id,COUNT(*) AS count FROM events WHERE last_seen_at>=datetime('now','-2 hours') GROUP BY source_id")}
        fresh_opportunities={r['source_id']:r['count'] for r in db.execute('''SELECT o.source_id,COUNT(*) AS count
            FROM opportunities o
            JOIN sources s ON s.id=o.source_id
            JOIN events e ON e.id=o.event_id
            LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
            WHERE o.status!='ignored'
              AND COALESCE(a.state,'queued') NOT IN ('stale','source_unavailable','retry','excluded')
              AND ((s.method='manual' AND o.status='verified'
                    AND datetime(o.buy_checked_at) BETWEEN datetime('now','-15 minutes') AND CURRENT_TIMESTAMP)
                OR (s.method!='manual' AND s.enabled=1 AND s.status='healthy'
                    AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                    AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                    AND (e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
                      OR a.state IN ('campaign_upcoming','campaign_active')
                      OR ((s.parser IN ('apple','mi','suning','lenovo','honor','kongfz')
                           OR s.method='taobao_alliance')
                          AND s.last_success BETWEEN datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes') AND CURRENT_TIMESTAMP))))
            GROUP BY o.source_id''')}
        source_coverage = [dict(source, match_count=source_match_counts.get(source['id'], 0),
                                quote_count=source_quote_counts.get(source['id'],0),
                                fresh_event_count=fresh_events.get(source['id'],0),
                                fresh_opportunity_count=fresh_opportunities.get(source['id'],0)) for source in sources]
        source_coverage.sort(key=lambda source: (-int(bool(source['enabled'])), -source['match_count'], source['platform'], source['name']))
        enabled_source_count = sum(bool(source['enabled']) for source in sources)
        matched_enabled_source_count = sum(bool(source['enabled']) and source['match_count'] > 0 for source in source_coverage)
        quoted_enabled_source_count = sum(bool(source['enabled']) and source['quote_count'] > 0 for source in source_coverage)
        alerts = sum(is_current_notice(row) for row in notice_rows(db, "pending"))
        benefit_link_counts=linked_benefit_counts(db,[row['id'] for row in rows])
    html = render_template("index.html", rows=rows, sources=sources,
                           benefit_link_counts=benefit_link_counts,
                           comparisons=comparisons,sort_mode=sort_mode,
                           search_assessments=search_assessments,quality_counts=quality_counts,
                           candidate_pool_count=candidate_pool_count, source_offer_count=source_offer_count,
                           alerts=alerts, query=query, category=category, status=status,
                           platform=platform, buy_platform=buy_platform,
                           buy_platform_label=buy_platform_label, marketplace_options=FILTER_OPTIONS,
                           marketplace_population_count=marketplace_population_count,
                           marketplace_selected_count=marketplace_selected_count,
                           marketplace_unidentified_count=marketplace_unidentified_count,
                           dedicated_taobao_source_count=dedicated_taobao_source_count,
                           budget_text=budget_text, minimum_discount_text=minimum_discount_text,
                           minimum_discount_cents=minimum_discount_cents,
                           offer_filter=offer_filter,topic=topic,resource=resource,
                           total=total, total_pages=total_pages, page_buttons=page_buttons,
                           page=page, has_next=page * page_size < total,
                           page_size=page_size, layout_mode=layout_mode,
                           source_coverage=source_coverage, enabled_source_count=enabled_source_count,
                           matched_enabled_source_count=matched_enabled_source_count,
                           quoted_enabled_source_count=quoted_enabled_source_count,
                           search_funnel=search_funnel,
                           candidate_only=candidate_only, show_archive=show_archive, view_mode=view_mode,
                           ready_only=ready_only, priced_only=priced_only,
                           source_comparison_deferred=source_comparison_deferred,
                           unpriced_current_count=unpriced_current_count)
    if page_cache_key is not None:
        put_page_html(page_cache_key, html)
    return html


def register(app):
    app.add_url_rule('/', endpoint='index', view_func=index, methods=['GET'])
