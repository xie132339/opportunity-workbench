# Remove repeated search-path evidence work

This ExecPlan is a living document maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome

Make the ordinary product-search path process each row's immutable source evidence once per request instead of re-parsing it in every matcher. The default official-offer page and current-lead page must render the same records, prices, platform attribution and optional maintained-band labels with materially less CPU time. No optimization may turn a missing price into a price, relax official-promotion admission or hide an unclassified official offer.

## Scope, constraints, and assumptions

- Work only on `feature/bootstrap`; no protected branch or historical business row changes.
- Optimize shared evidence/matching code, not an endpoint-specific feature flag or a new operational configuration.
- The per-request cache may hold only data already stored on the in-memory row. It must invalidate if `metadata_json` changes and must not persist coupon validity across requests.
- Existing source comparison remains an optional diagnostic; its >100-row deferral is already covered by the prior fix.

## Context and orientation

`routes/search.py:index` retrieves current rows, uses `link_resolution` and `marketplace_identity` for purchase-platform evidence, then `attach_offer_data` for `autoreview.offer_summary`, `product_search.listing_quote`, `product_search.is_promotional_price_candidate` and `promotion_admission`. Each of these calls `services.taobao_alliance.validated_offer`, which JSON-decodes and revalidates the same raw material. A real 5,048-row database profile measured 4,252 current rows and 18,396 `validated_offer` calls in one default-page request.

## Progress

- [x] (2026-10-11 CST) Measured the real default page: 10.44 seconds under `cProfile`; `attach_offer_data` and its downstream `offer_summary` were the largest CPU consumers.
- [x] (2026-10-11 CST) Found repeated validation of the same source field across platform matching, offer summary, quote construction, promotion detection and admission.
- [x] (2026-10-11 CST) Added a row-local source-evidence cache and replaced all search-path Alliance consumers.
- [x] (2026-10-11 CST) Added cache invalidation and deferred-render regression coverage; focused suite passed (73 tests).
- [x] (2026-10-11 CST) Deferred render-only text diagnostics until after pagination whenever source comparison is already deferred. Re-profiled the real default page: 6,506,246 calls in 2.345 seconds, HTTP 200, with no deferred placeholder visible.
- [x] (2026-10-11 CST) Complete regression suite passed: 311 tests in 2.778 seconds.
- [x] (2026-10-11 CST) Restarted local server and exercised the default, current, card and second-page routes. All returned HTTP 200 in 0.69–0.87 seconds; visible pages contained no deferred placeholder and card diagnostics were present.
- [x] (2026-10-11 CST) Kept the remaining SQLite work unchanged: it is necessary joined-source retrieval and the measured unprofiled requests are sub-second, so an index/caching migration is not justified.

## Plan of work

1. Add a shared row-aware accessor in `services/taobao_alliance.py`. It calls the existing validator once for an unchanged in-memory row and revalidates when the stored metadata changes. Keep `validated_offer` intact for persistence and explicit-time callers.
2. Replace search-path consumers in `services/marketplace_identity.py`, `autoreview.py` and `services/product_search.py` with the accessor. Preserve every existing acceptance predicate and label.
3. When broad comparison is already deferred, construct only the official-evidence fields needed for filtering and sorting; regenerate coupon wording, arithmetic and sync diagnostics after pagination for visible records only.
4. Test repeated use, metadata invalidation, official quote display, conditional SKU detail behavior, maintained-band matching and large-list comparison deferral.
5. Re-profile the default and current pages using the real database. If SQL remains a material hotspot, inspect query plans before adding a narrowly justified index.

## Validation and acceptance

- Focused unit test confirms one row uses one validation pass across multiple consumers and revalidates after metadata replacement.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests` passes.
- Real default and current page requests return HTTP 200, preserve the optional-calibration copy, and do not invoke the deferred peer comparison for a large result set.
- Real profile records fewer `validated_offer` invocations than 18,396 for the same database snapshot. A static cache or an HTTP 200 alone is not sufficient.
- The browser-visible page must not contain the internal deferred-diagnostic placeholder; all selected rows must be hydrated before rendering.

## Surprises and discoveries

- The response time improved from about 8.5 seconds to 4.6–4.8 seconds after deferring the optional source-comparison graph, but CPU profiling still found 35.6 million calls. The remaining main cost is repeated evidence parsing, not local price-band assessment.
- After the row cache, the same profile still made 32.75 million calls in 9.44 seconds because `offer_summary`, `discount_audit` and `promotion_sync_status` ran for every pre-pagination row.
- After delayed diagnostics, the real default page made 6.51 million calls in 2.35 seconds under `cProfile`; SQLite joins (0.78 seconds) are now the largest cost. The page did not contain the deferred diagnostic text.

## Decision log

- 2026-10-11: remove repeated per-row work before adding indexes. Caching an unchanged request-local source field preserves the existing validator and avoids a speculative schema/index migration.
- 2026-10-11: treat text arithmetic and synchronization explanations as render diagnostics, while keeping source validation, quote construction, promotion admission, filtering and sorting on the full corpus. This applies only when comparison diagnostics are already deferred; ready/comparison views retain full corpus diagnostics.

## Idempotence and recovery

- The cache is attached only to the transient row dictionary and is never written to SQLite. Removing it restores the previous call pattern with no data recovery step.

## Outcomes and retrospective

Delivered a request-local source-evidence cache plus render-lazy diagnostics for
broad searches.  The cache invalidates when `metadata_json` changes and never
persists a price or coupon decision.  The broad list still validates every
source record, builds quotes, checks official promotion evidence, filters and
sorts the full corpus.  Only text explanations that are not admission inputs
are delayed until the final page.

Acceptance evidence: focused 73-test suite passed; full 311-test suite passed;
the real 5,048-record default route dropped from 35.6 million calls / 10.44
seconds to 6.51 million calls / 2.35 seconds under `cProfile`.  Restarted HTTP
checks returned 200 for default list (0.87 seconds), current list (0.69
seconds), card view (0.77 seconds) and page two (0.69 seconds).  No visible
response included the internal deferred-diagnostic marker.
