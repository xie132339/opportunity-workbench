# Build internally maintained price baselines

This ExecPlan is a living document maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome

Turn the current “official Taobao promotion observation” into a transparent comparison against price bands maintained inside this workbench. An operator can define a concrete brand/product/package rule and its normal low, typical, high and deal thresholds. When an incoming Taobao official promotion satisfies that exact maintained rule and its unit can be calculated, the list and detail show the current normalized unit price, the matched baseline version and the resulting maintained-rule verdict. A missing or ambiguous match never produces a verdict.

## Scope, constraints, and assumptions

- Work only on `feature/bootstrap`; `main` and other protected branches remain untouched.
- The user explicitly chose internal maintenance over external price acquisition. No provider, scraper, third-party price claim or generated price is used as a baseline.
- Existing official-promotion admission stays unchanged. This work does not turn an alliance promotion price into an account checkout price.
- A static operator-maintained threshold is not evidence of a cross-market lowest price. UI wording will say `按维护基准` and retain the baseline version/effective time.
- Preserve existing source/event/opportunity history. Schema changes append configuration and audit records only.
- Reuse the existing strict measurement parser, but let each maintained baseline own its unit and base quantity. Category rules only provide dynamic classification.

## Context and orientation

`services/taobao_alliance.py` produces official promotion observations. `routes/search.py` prepares default search rows and `templates/index.html` displays them. `benchmark_categories` supplies dynamic product classification. `services/category_comparison_rules.py` supplies the strict measurement parser. The maintained band owns the actual unit and base quantity, because one category can contain “每 100 抽”、 “每包” and fixed-piece products. Its disabled external reference policy remains separate from this flow. The new logic therefore belongs beside category configuration, not in the Taobao adapter.

A maintained baseline contains: category, display name, all required title tokens (brand/product/variant), unit mode and unit count, low/typical/high/deal price bands in integer cents for that exact unit, effective time and enabled state. For mass, volume and count rules, the incoming title must yield one unambiguous measured quantity under the baseline-owned unit rule. `exact_spec` baselines require an exact configured specification signature. All required tokens must appear; title similarity is insufficient.

## Progress

- [x] (2026-10-11 15:xx CST) Captured the pre-change boundary: official promotion discovery passed, while AC-05 had no local price-band decision path.
- [x] (2026-10-11 15:xx CST) Found existing category/unit rules and the deliberately disabled external-reference policy.
- [x] (2026-10-11 16:xx CST) Added idempotent tables and append-only audit trail for maintained price baselines.
- [x] (2026-10-11 16:xx CST) Implemented strict baseline validation, matching, integer-cent normalization and fail-closed verdicts.
- [x] (2026-10-11 16:xx CST) Added baseline/category management UI and integrated verdicts into list/detail display and default ordering.
- [x] (2026-10-11 16:xx CST) Updated product contract, acceptance documentation and regression tests.
- [x] (2026-10-11 16:xx CST) Ran 306 tests; migrated and restarted the local service; verified the real database integrity and management page.

## Plan of work

1. **Configuration storage.** Add a `maintained_price_baselines` table and append-only audit table through `db.initialize()`. Store all money as cents, retain `version`, `enabled`, `effective_from` and `updated_at`, and use no destructive migration. Validate price ordering: `deal <= low <= typical <= high`.
2. **Deterministic assessment service.** Add `services/maintained_price_baselines.py`. It loads enabled rules, validates required terms and unit configuration, matches only the resolved category and all required normalized title terms, derives an exact title-based measurement under the baseline-owned unit rule, calculates the displayed price for the configured base quantity, and returns either a complete band verdict or a precise non-match reason. It will never assign a verdict from a missing amount, ambiguous measure, invalid rule or non-official promotional observation.
3. **Operator experience.** Extend `/price-benchmarks` with add/edit/enable controls for maintained baselines. No external-provider information appears in this management workflow or participates in this feature. Add POST validation and audit rows for each edit.
4. **Product experience.** In `routes/search.py` apply assessments after official quote preparation. Show the current unit price, configured bands, saved-versus-typical amount and `按维护基准：可捡漏/优惠/常规/偏高` status. The default list ranks matched lower-price verdicts before equally eligible unmatched promotions; it does not hide unmatched official promotions.
5. **Contract and tests.** Update `docs/current-product-contract.md` and acceptance criteria to distinguish maintained-rule verdicts from independent-market claims. Add tests for valid count/mass/volume calculations, exact-token matching, invalid band ordering, expired/disabled baselines, audit/version behavior, UI integration and no verdict when data is incomplete.

## Validation and acceptance

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests` passes.
- Focused baseline tests cover a real-shaped paper rule (e.g. 100抽 base unit and 6-pack title), one mass or volume rule, and each fail-closed path.
- A Flask test-client check confirms `/price-benchmarks` saves a baseline only with valid ordered bands and writes an audit row.
- A list-route test confirms a matched official Taobao promotion is labeled with its maintained-baseline verdict and an unmatched/ambiguous item shows no verdict.
- Database `PRAGMA quick_check` passes after repeated initialization; business event/opportunity counts do not change.
- If local runtime credentials are available, load `/price-benchmarks` and a matching search after restart; otherwise label the UI runtime check unexercised.

## Surprises and discoveries

- `benchmark_categories` already supplies category matching and strict mass/volume/count normalization. Reusing it avoids a competing per-brand unit parser.
- `offer_summary()` leaves `selected_spec` empty for non-SKU Taobao material. The maintained rule may therefore use the visible title only when all required identity tokens and one unambiguous configured measurement are present; it must disclose that as `标题规格匹配` rather than a bound SKU.

## Decision log

- 2026-10-11: use operator-maintained bands, not a self-derived average of current promotions. A circular average could make an entire promotion feed define its own “low price.”
- 2026-10-11: use `按维护基准：可捡漏` instead of `已证实全网最低价`. The former is a reproducible decision under the user-maintained rule; the latter needs independent market evidence.
- 2026-10-11: no seed price numbers. Shipping guessed normal-price data would create false bargain signals. The product will offer the management workflow and wait for explicit maintained values.

## Idempotence and recovery

`CREATE TABLE IF NOT EXISTS` plus `INSERT OR IGNORE` only adds structures. Re-running initialization does not alter event history. Baseline edits are versioned and audit recorded; disabling a baseline stops verdicts without deleting prior configuration. If a rule is configured incorrectly, edit or disable it; old audit records remain for explanation.

## Outcomes and retrospective

Completed on 2026-10-11. `unittest discover -s tests` passed 306 tests, including 9 dedicated maintained-baseline tests. Real database initialization created both baseline tables without changing the 5,048 event or opportunity records; `PRAGMA quick_check` returned `ok`. The service and Taobao worker were restarted successfully and `/price-benchmarks` rendered the local-maintenance state.

The live database intentionally has zero baseline rows after migration. No real brand threshold was guessed or seeded. The next operating action is to add a concrete baseline in the page and replay a matching official Taobao offer; that is an operational data-validation step, not a code blocker.
