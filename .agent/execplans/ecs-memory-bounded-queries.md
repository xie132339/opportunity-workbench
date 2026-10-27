# 让 2 GiB ECS 的采集和查询按批处理

This ExecPlan is a living document maintained according to `~/.codex/PLANS.md`.

## Purpose and observable outcome

启动、淘宝采集复审和默认优惠搜索不再因业务库增长而一次装入全量记录。ECS 能完成采集周期，列表可访问，网页和采集服务不触发 OOM 或异常重启。

## Scope, constraints, and assumptions

- 本轮只处理已定位的全量读取路径；保留原有优惠准入、排序和查重口径。
- 开发 worktree 为 `/tmp/opportunity-memory-audit` 的非保护分支 `feature/memory-audit`；原本地 `feature/bootstrap` 有用户改动 `services/product_search.py`，不得覆盖。
- 线上为 `/application` 的非保护分支 `feature/bootstrap`，系统盘快照 `s-uf6bvqzyfbs9qipz80fj` 已存在。部署前核对分支和干净工作区。
- 生产 SQLite 不复制到仓库，不删除历史数据；凭据保持在服务器忽略的 `.env`。

## Context and orientation

`db.initialize()` 在网页和 worker 启动时扫描旧记录；`autoreview.review_all()` 复审来源记录；`routes/search.py` 构造优惠列表候选和漏斗；`price_claim_evidence()` 汇总报价。线上库 2026-10-27 实测约 2.4 GB、124219 条机会。前轮已将 `discount_calibration.calibrate()` 与 `shop_reputation.load_shop_stats()` 改为游标遍历，并给 systemd 设置 600/850 MiB 内存阈值与 1 GiB Swap。本轮继续消除其他同类物化。

## Progress

- [x] (2026-10-27 13:10 CST) 核对非保护分支、原本地改动、线上服务与当前数据量。
- [x] (2026-10-27 13:16 CST) 启动迁移与复审改成按主键分批；报价汇总改游标；搜索候选改流式 ID 与固定大小 Top N，漏斗只查询必要列并逐行汇总。
- [x] (2026-10-27 13:16 CST) 跨 500/200 条边界测试通过；完整测试 469 项中 2 项失败，改动前版本复现同样失败。
- [x] (2026-10-27 13:19 CST) 提交 `3fd43c3`、推送 `feature/bootstrap` 并快进部署到 `/application`；依次重启网页和采集。
- [x] (2026-10-27 13:21 CST) 真实登录后查询列表与采集一轮完成；两服务 `NRestarts=0`、`oom=0`，网页公网登录正常。
- [x] (2026-10-27 13:25 CST) 补查首页新鲜机会统计，发现并修复遗漏的全量 event ID 列表，改为 900 条一批；跨批次 905 条有效机会计数测试通过。
- [x] (2026-10-27 13:26 CST) 更新 `docs/core-acceptance.md` AC-07，并同步 Obsidian 实施日志和核心台账；首次列表耗时已标记为未通过。
- [x] (2026-10-27 13:29 CST) 补充提交 `f704763` 推送并部署 `/application`，顺序重启两服务；登录后“抽纸”列表 200/8.369 秒、网页内存峰值约 532 MiB；采集首轮 `selected=200`、两服务 `NRestarts=0` 与 `oom=0`。

## Plan of work

1. 对历史库读取路径用主键分批或 SQLite 游标代替无界 `fetchall()`；固定 200/500 条批量，记录所有行而不跳页。
2. 默认优惠预选以 SQLite `UNION` 游标提供唯一 event ID，每次最多 900 ID；用 `heapq.nlargest` 只留 1500 个轻量候选，不排序全量 Python 字典。
3. 回归测试保留商品准入和结果排序，线上确认服务读到新代码并完成一轮采集。

## Validation and acceptance

- `/Applications/work/gitRepo/opportunity-workbench/.venv/bin/python -B -m unittest discover -s tests`：当前 469 项，2 项 `test_price_admission` 失败；在未改动基线也复现，尚未由本轮修复。
- `test_memory_boundaries.py`：520 条启动迁移、450 条复审跨批次且保留查重结果，2 项通过。
- `test_search_freshness_two_stage.py`：3 项通过，其中 905 条有效机会跨 900 ID 批次且正确排除 2 条；`test_core_rules.py`：33 项通过。
- 线上已执行：登录后“抽纸”列表和无关键词列表均返回 200，采集输出 `selected=200` 后进入 `app.py:201` 的等待；网页、worker 均 `NRestarts=0`，两个 cgroup 的 `oom=0`。
- 补充版本 `f704763`：实际首页统计代码回放，登录后“抽纸”列表 200/8.369 秒、返回 251731 字节；worker 输出 200 条复审，两个 cgroup 未出现 OOM 或异常重启。
- 性能边界：“抽纸”首次 17.437 秒；已缓存翻页 0.87 秒；无关键词第 2 页首次 14.062 秒。内存未失控，但首次请求速度未达到用户此前期望。

## Surprises and discoveries

- 历史库比前轮更大，机会数从 110288 增至 124219；不能依赖固定历史规模。
- `test_price_admission` 的两项失败在 `8f388d6` 基线完全相同，不能算本轮回归。
- `source_provenance_evidence()` 等完整审计报告刻意返回全量明细；本轮不改变该报告的业务语义，不将其混同普通列表路径。

## Decision log

- 2026-10-27：保留最多 1500 个默认优惠候选的既有产品边界，只改变候选预取的内存复杂度；不得悄悄缩小准入范围。
- 2026-10-27：诊断聚合只取 ID 与状态，完整报价记录留给被展示的候选。

## Idempotence and recovery

分批按严格递增主键推进；重复启动或复审安全。部署失败时不碰 SQLite 数据，用现有快照与上一 Git 提交恢复服务代码。不能用重启或 Swap 掩盖持续增长的查询内存。

## Outcomes and retrospective

已交付启动迁移、复审、报价汇总、默认优惠预选、漏斗统计及首页新鲜机会计数的有界读取，并在 124219 条生产记录上验证采集完整周期与网页列表无 OOM。首次列表仍需独立性能诊断；历史审计视图的全量明细输出不属于本轮普通列表路径，不能据此宣称全项目所有查询均有界。
