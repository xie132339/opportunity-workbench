"""Search fresh source leads and optionally filter for comparison readiness."""
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
                                     is_promotional_price_candidate, promotion_admission)
from services.source_provenance import annotate_search_sources, build_search_source_index
from services.search_diagnostics import summarize_search_funnel
from services.public_campaign import parse_public_campaign
from services.marketplace_identity import (
    FILTER_OPTIONS, attach_product_marketplace, marketplace_matches_filter,
)
from services.maintained_price_baselines import assess_maintained_baseline, load_baselines
from services.taobao_alliance import validated_offer_for_row
from services.search_result_cache import get as get_prepared_search, put as put_prepared_search


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
                             sort_mode, maintained_baselines, funnel_scope_sql,
                             funnel_scope_args):
    """Prepare the complete result set once before page-specific rendering.

    Promotion admission and comparison ordering apply to the whole matching
    corpus.  Keeping this work here makes it explicit that page slicing is a
    rendering concern, and lets the caller reuse this prepared result safely.
    """
    raw_rows = [dict(row) for row in db.execute(sql + order_by, args)]
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
        row['maintained_baseline'] = assess_maintained_baseline(
            row, row['offer_brief'], row.get('display_quote'), maintained_baselines)
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
        all_rows = [row for row in all_rows if row.get('public_offer_eligible')]
    if budget is not None:
        all_rows = [row for row in all_rows if row.get('display_quote') and row['display_quote'].get('kind') in {
            'source_claim', 'official_promotion_observation'} and row['display_quote']['amount_cents'] <= budget]
        source_match_counts = {}
        source_quote_counts = {}
        for matched_row in all_rows:
            source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            source_quote_counts[matched_row['source_id']] = source_quote_counts.get(matched_row['source_id'], 0) + 1
    if sort_mode == 'comparison':
        all_rows.sort(key=lambda row: (row.get('maintained_baseline', {}).get('rank', 0),
                                       comparisons.get(row['id'], {}).get('rank', 0), row['id']), reverse=True)
    elif sort_mode == 'latest':
        all_rows.sort(key=lambda row: (row.get('published_at') or row.get('last_seen_at') or '', row['id']), reverse=True)
    elif sort_mode in ('price_low', 'price_high', 'paper_unit_low'):
        priced, unpriced = [], []
        for row in all_rows:
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
                    reverse=sort_mode == 'price_high')
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
    sort_mode = request.args.get("sort", "comparison")
    if sort_mode not in ("comparison", "latest", "price_low", "price_high", "paper_unit_low"):
        sort_mode = "comparison"
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
    show_archive = view_mode == "all"
    ready_only = view_mode == "ready"
    priced_only = view_mode == "offers"
    try:
        budget = cents(budget_text) if budget_text else None
    except ValueError:
        budget = None
    sql = """SELECT o.*,s.platform,s.category AS source_category,s.status AS source_status,s.method AS source_method,s.parser AS source_parser,
             s.enabled AS source_enabled,s.interval_minutes AS source_interval,s.name AS source_name,
             s.last_success AS source_last_success,
             e.is_baseline,e.published_at,e.last_seen_at,e.url AS event_url,e.snippet,e.metadata_json,
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
            sql += " AND (o.title LIKE ? OR (COALESCE(e.snippet,'') LIKE ? AND (COALESCE(e.snippet,'') LIKE '%元%' OR COALESCE(e.snippet,'') LIKE '%¥%' OR COALESCE(e.snippet,'') LIKE '%￥%')))"
            args.extend(("%" + query + "%", "%" + query + "%"))
            if query == '纸':
                # Common non-paper compound terms in deal feeds.
                for unrelated_title_term in ('纸皮', '响纸'):
                    sql += " AND o.title NOT LIKE ?"
                    args.append('%' + unrelated_title_term + '%')
        else:
            sql += " AND (o.title LIKE ? OR COALESCE(e.snippet,'') LIKE ?)"
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
                prepared['maintained_baseline'] = assess_maintained_baseline(
                    prepared, prepared['offer_brief'], prepared.get('display_quote'), maintained_baselines)
            total=len(prepared_candidates)
            rows = prepared_candidates[(page - 1) * page_size:page * page_size]
            search_assessments={}
            quality_counts=dict(search_ready=0,comparable=0,source_claim_low=0,excluded=0,
                                displayable_quote=sum(bool(row.get('display_quote')) for row in prepared_candidates),
                                promotional_candidate=sum(bool(row.get('promotional_candidate')) for row in prepared_candidates),
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
                'standard-search-v3', database_file,
                query, category, status, platform,
                buy_platform, offer_filter, topic, resource, budget, sort_mode,
                view_mode,
            )
            prepared = get_prepared_search(prepared_key)
            if prepared is None:
                prepared = prepare_standard_results(
                    db, sql=sql, args=args, order_by=order_by, topic=topic,
                    buy_platform=buy_platform, show_archive=show_archive,
                    ready_only=ready_only, priced_only=priced_only, budget=budget,
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
    return render_template("index.html", rows=rows, sources=sources,
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
                           budget_text=budget_text,
                           offer_filter=offer_filter,topic=topic,resource=resource,
                           total=total, page=page, has_next=page * page_size < total,
                           page_size=page_size, layout_mode=layout_mode,
                           source_coverage=source_coverage, enabled_source_count=enabled_source_count,
                           matched_enabled_source_count=matched_enabled_source_count,
                           quoted_enabled_source_count=quoted_enabled_source_count,
                           search_funnel=search_funnel,
                           candidate_only=candidate_only, show_archive=show_archive, view_mode=view_mode,
                           ready_only=ready_only, priced_only=priced_only,
                           source_comparison_deferred=source_comparison_deferred,
                           unpriced_current_count=unpriced_current_count)

def register(app):
    app.add_url_rule('/', endpoint='index', view_func=index, methods=['GET'])
