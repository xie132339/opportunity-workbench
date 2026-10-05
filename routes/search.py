"""Search fresh source leads and optionally filter for comparison readiness."""
from datetime import datetime, timezone
from flask import render_template, request
from db import connect
from comparison import load_comparisons, assess_readiness
from autoreview import classify_catalog_observation, offer_summary
from benefits import linked_counts as linked_benefit_counts
from offer import RESOURCE_LABELS, TOPIC_LABELS, paper_package_prices
from link_resolution import enrich as enrich_links
from services.money import cents
from services.opportunity_analysis import is_current_notice, is_evidence_candidate, notice_rows
from services.source_freshness import CATALOG_LISTING_PARSERS

def index():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    platform = request.args.get("platform", "").strip()
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
    # Default to the current source inbox so incomplete fresh records stay visible.
    # The explicit "ready" mode applies the stricter product-comparison gate.
    view_mode = request.args.get("view") or "current"
    if view_mode not in ("ready", "current", "all"):
        view_mode = "current"
    show_archive = view_mode == "all"
    ready_only = view_mode == "ready"
    try:
        budget = cents(budget_text) if budget_text else None
    except ValueError:
        budget = None
    sql = """SELECT o.*,s.platform,s.status AS source_status,s.method AS source_method,s.parser AS source_parser,
             s.enabled AS source_enabled,s.interval_minutes AS source_interval,s.name AS source_name,
             s.last_success AS source_last_success,
             e.is_baseline,e.published_at,e.last_seen_at,e.snippet,e.metadata_json,
             a.state AS auto_state,a.reason AS auto_reason,a.advertised_cents,
             a.specification AS auto_specification,a.conditions AS auto_conditions,a.detail_json,
             a.checked_at AS review_checked_at,a.detail_checked_at,a.detail_error
             FROM opportunities o LEFT JOIN sources s ON s.id=o.source_id
             LEFT JOIN events e ON e.id=o.event_id
             LEFT JOIN auto_reviews a ON a.opportunity_id=o.id WHERE 1=1"""
    args = []
    if topic in TOPIC_LABELS:
        sql += ' AND o.topic=?'
        args.append(topic)
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
    if budget is not None:
        sql += " AND a.advertised_cents IS NOT NULL AND a.advertised_cents<=? AND a.state NOT IN ('conflict','excluded')"
        args.append(budget)
    if status:
        sql += " AND o.status=?"
        args.append(status)
    else:
        sql += " AND o.status!='ignored'"
    if not show_archive:
        if not candidate_only:
            # Product search and the current lead inbox start from fresh purchase-like
            # records. The explicit ready view applies the shared evidence gate below.
            sql += " AND o.resource_kind IN ('purchase','unknown')"
        sql += " AND COALESCE(a.state,'queued') NOT IN ('stale','source_unavailable','retry','excluded')"
        sql += """ AND ((s.method='manual' AND o.status='verified'
                         AND datetime(o.buy_checked_at) BETWEEN datetime('now','-15 minutes') AND CURRENT_TIMESTAMP)
                     OR (s.method!='manual' AND s.enabled=1 AND s.status='healthy'
                         AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                         AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                         AND (e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
                              OR (s.parser IN ('apple','mi','suning','lenovo','honor','kongfz')
                                  AND s.last_success BETWEEN datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes') AND CURRENT_TIMESTAMP))) )"""
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    with connect() as db:
        comparisons = load_comparisons(db)
        link_cache={r['url']:dict(r) for r in db.execute('SELECT * FROM link_resolutions')}
        order_by = """ ORDER BY CASE o.category
            WHEN '零售优惠' THEN 1 WHEN '二手与闲置' THEN 2
            WHEN '供货与清仓' THEN 3 WHEN '新品与补货' THEN 4
            WHEN '拍卖与资产' THEN 5 WHEN '服务与合作' THEN 6 ELSE 7 END,
            o.created_at DESC,o.id DESC"""
        if candidate_only:
            rules = db.execute("SELECT * FROM strategies WHERE enabled=1").fetchall()
            candidates = []
            for row in db.execute(sql + " AND o.status='verified'" + order_by, args):
                quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (row["id"],)).fetchall()
                if is_evidence_candidate(row, quotes, rules):
                    candidates.append(row)
            total = len(candidates)
            source_match_counts = {}
            for matched_row in candidates:
                source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            rows = [dict(row) for row in candidates[(page - 1) * page_size:page * page_size]]
            search_assessments={}
            quality_counts=dict(search_ready=0,comparable=0,source_claim_low=0,excluded=0)
            candidate_pool_count=total
            source_offer_count=total
        else:
            all_rows = [enrich_links(dict(row),link_cache) for row in db.execute(sql + order_by,args)]
            if not show_archive:
                # A live catalogue row may have an old saved ``missing_time``
                # result from the former post-only rule. Re-evaluate that row
                # for display against its current successful snapshot; do not
                # write the refreshed result into the evidence ledger here.
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
            source_match_counts = {}
            for matched_row in all_rows:
                source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            # Keep every source record visible. Similar title/price text is not
            # enough evidence to merge channels or merchants into one offer.
            source_offer_count = candidate_pool_count = len(all_rows)
            for row in all_rows:
                row['catalog_listing'] = row.get('source_parser') in CATALOG_LISTING_PARSERS
                platform_name = (row.get('platform') or '').strip()
                feed_name = (row.get('source_name') or '').strip()
                source_label = f'{platform_name} · {feed_name}' if platform_name and feed_name and feed_name != platform_name else (platform_name or feed_name or '人工录入')
                row['source_channels'] = [source_label]
                row['source_count'] = 1
            search_assessments={row['id']:assess_readiness(row,comparisons.get(row['id'],{})) for row in all_rows}
            quality_counts=dict(
                search_ready=sum(bool(value['search_ready']) for value in search_assessments.values()),
                comparable=sum(bool(value['comparable']) for value in search_assessments.values()),
                source_claim_low=sum(bool(value['source_claim_low']) for value in search_assessments.values()))
            quality_counts['excluded']=candidate_pool_count-quality_counts['search_ready']
            if ready_only and not show_archive:
                all_rows=[row for row in all_rows if search_assessments[row['id']]['search_ready']]
                source_match_counts = {}
                for matched_row in all_rows:
                    source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            total=len(all_rows)
            if sort_mode == 'comparison':
                all_rows.sort(key=lambda r: comparisons.get(r['id'],{}).get('rank',0),reverse=True)
            elif sort_mode == 'latest':
                all_rows.sort(key=lambda r: (r.get('published_at') or r.get('last_seen_at') or '', r['id']), reverse=True)
            elif sort_mode in ('price_low', 'price_high', 'paper_unit_low'):
                priced, unpriced = [], []
                for row in all_rows:
                    brief = offer_summary(row['title'], row['url'], row.get('snippet'), row.get('auto_conditions'),
                                          row.get('metadata_json'), row.get('detail_json'),
                                          dict(checked_at=row.get('review_checked_at'),
                                               detail_checked_at=row.get('detail_checked_at'),
                                               detail_error=row.get('detail_error')))
                    value = brief.get('total_cents')
                    if value is None:
                        value = row.get('advertised_cents')
                    if sort_mode == 'paper_unit_low':
                        title = brief.get('title') or row['title']
                        paper = paper_package_prices(title, brief.get('total_cents'), brief.get('quantity'))
                        value = paper.get('per_pack_cents') if paper else None
                    if value is None:
                        unpriced.append(row)
                    else:
                        row['_sort_price_cents'] = value
                        priced.append(row)
                priced.sort(key=lambda r: (r['_sort_price_cents'], r.get('published_at') or r.get('last_seen_at') or '', r['id']),
                            reverse=sort_mode == 'price_high')
                for row in priced:
                    row.pop('_sort_price_cents', None)
                all_rows = priced + unpriced
            rows=all_rows[(page-1)*page_size:page*page_size]
        sources = [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY id").fetchall()]
        source_coverage = [dict(source, match_count=source_match_counts.get(source['id'], 0)) for source in sources]
        source_coverage.sort(key=lambda source: (-int(bool(source['enabled'])), -source['match_count'], source['platform'], source['name']))
        enabled_source_count = sum(bool(source['enabled']) for source in sources)
        matched_enabled_source_count = sum(bool(source['enabled']) and source['match_count'] > 0 for source in source_coverage)
        alerts = sum(is_current_notice(row) for row in notice_rows(db, "pending"))
        benefit_link_counts=linked_benefit_counts(db,[row['id'] for row in rows])
    return render_template("index.html", rows=rows, sources=sources,
                           benefit_link_counts=benefit_link_counts,
                           comparisons=comparisons,sort_mode=sort_mode,
                           search_assessments=search_assessments,quality_counts=quality_counts,
                           candidate_pool_count=candidate_pool_count, source_offer_count=source_offer_count,
                           alerts=alerts, query=query, category=category, status=status,
                           platform=platform, budget_text=budget_text,
                           offer_filter=offer_filter,topic=topic,resource=resource,
                           total=total, page=page, has_next=page * page_size < total,
                           page_size=page_size, layout_mode=layout_mode,
                           source_coverage=source_coverage, enabled_source_count=enabled_source_count,
                           matched_enabled_source_count=matched_enabled_source_count,
                           candidate_only=candidate_only, show_archive=show_archive, view_mode=view_mode, ready_only=ready_only)

def register(app):
    app.add_url_rule('/', endpoint='index', view_func=index, methods=['GET'])
