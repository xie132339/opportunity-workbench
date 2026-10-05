# Split Workbench Code by Business Responsibility

This ExecPlan is a living document maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome

Reduce the Flask entry file from a mixed 1,440-line application into a small bootstrap/CLI module. A maintainer should be able to locate route handlers by business area, profitability/freshness rules in domain services, and alert dispatch in notification services without changing current URLs, templates, database behavior, or business results.

## Scope, constraints, and assumptions

- Work only in non-protected `feature/bootstrap`; leave `main` unchanged.
- Preserve every HTTP path, method, endpoint name, CSRF behavior, CLI command, and worker action.
- Keep existing business algorithms unchanged; this is a responsibility refactor, not a scope expansion or rule change.
- Reuse existing Flask and unittest dependencies. No new dependency or new test framework.
- Keep `from app import app` and existing business helper imports working where practical; update tests that patch internals to patch the module that now owns the dependency.
- Do not touch live user database data or run external collectors/notifications.

## Context and orientation

`app.py` currently has 1,440 lines: environment and Flask initialization, money conversion, profit/freshness/candidate rules, notification fan-out, and route handlers for product search, benefits, sources, Xianyu, ledger, and notifications. Existing business engines already live in `comparison.py`, `benefits.py`, `scanner.py`, `merchant_verification.py`, `price_history.py`, `notifier.py`, and other top-level domain modules. No `.codegraph/` directory is present, so use focused Python AST and `rg` inspection. Templates depend on unprefixed Flask endpoint names, so route registration must preserve those names.

## Progress

- [x] (2026-10-05 10:57 Asia/Shanghai) Inspected the route inventory and compatibility points. Original `app.py` was 1,440 lines; templates and tests use flat endpoint names; no `.codegraph/` exists.
- [x] (2026-10-05 10:57 Asia/Shanghai) Moved currency/cost utilities, freshness/profitability gates, and alert dispatch into `services/`.
- [x] (2026-10-05 10:57 Asia/Shanghai) Split request handlers into search, opportunity, benefits, sources, Xianyu, ledger, notifications, and system route modules. Explicit endpoint registration preserves all 34 application endpoints plus Flask's static endpoint.
- [x] (2026-10-05 10:57 Asia/Shanghai) Existing full suite: 177 tests pass. Isolated test-client smoke: 10 pages return 200; four POST routes reject missing CSRF with 400; flat endpoint `url_for()` checks pass. An AST comparison against `HEAD:app.py` confirms all 34 `(path, endpoint, method)` triples match exactly. An isolated CLI invalid-command smoke prints the original usage and exits 1. `py_compile` and `git diff --check` pass.
- [x] (2026-10-05 10:57 Asia/Shanghai) Recorded evidence in `docs/core-acceptance.md` and `docs/environment.md`; linked working logs already exist in Obsidian and are updated for this milestone.

## Plan of work

1. Extract currency conversion and costs to `services/money.py`; freshness, resale, and public profit assessments to `services/opportunity_analysis.py`; alert fan-out to `services/alert_dispatch.py`.
2. Split Flask handlers into `routes/catalog.py`, `routes/benefits.py`, `routes/sources.py`, `routes/xianyu.py`, `routes/ledger.py`, `routes/notifications.py`, and `routes/system.py`. Register with `add_url_rule(..., endpoint=<existing endpoint>)` so all template `url_for()` calls retain their names.
3. Keep `app.py` as environment bootstrap, app wiring, global template filters/context/CSRF hook, CLI and worker orchestration. Re-export domain helpers needed by current Python callers.
4. Update tests' monkeypatch targets only where the owning module moved. Run the entire existing suite and request each page/form route in an isolated DB-backed test client; fix regressions before recording completion.

## Validation and acceptance

- `PYTHONPYCACHEPREFIX=/private/tmp/workbench-split-pycache .venv/bin/python -m unittest discover -s tests`
- `PYTHONPYCACHEPREFIX=/private/tmp/workbench-split-pycache .venv/bin/python -m py_compile app.py services/*.py routes/*.py`
- `git diff --check`
- Isolated Flask test client: confirm all nine primary pages return 200, form endpoints preserve their existing paths and CSRF behavior, `url_for()` references still resolve, and CLI help/invalid command path remains intact.
- Compare `app.py` line count with the 1,440-line baseline and list per-domain module sizes.

## Surprises and discoveries

Record only evidence discovered during the refactor.

## Decision log

- 2026-10-05: Preserve flat endpoint names by using explicit `app.add_url_rule()` registrations from each route module; this avoids rewriting numerous templates and prevents URL changes as a side effect of moving handlers.

## Idempotence and recovery

The refactor is source-only and repeatable. Keep all changes on `feature/bootstrap`; if a route import or registration fails, use the existing suite to localize it and restore only that module from the branch diff if necessary. Do not alter DB files, the running service, or protected refs.

## Outcomes and retrospective

Delivered. `app.py` is 130 lines (from 1,440); HTTP behavior and business logic remained green under 177 regressions and isolated page/CSRF/URL checks. Largest route modules are `routes/search.py` (260 lines) and `routes/opportunities.py` (234 lines), each scoped to a distinct user workflow. This was a responsibility refactor only: it does not improve source coverage or complete the bargain/profit objective. No live service, collector, notification, database, or external source was exercised.
