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
- [ ] 将代码提交、推送并部署到 `/application`，确认实际版本和两服务状态。
- [ ] 用公网真实列表请求、采集周期和 cgroup OOM/重启计数验证；更新核心验收证据与 Obsidian 交接记录。

## Plan of work

1. 对历史库读取路径用主键分批或 SQLite 游标代替无界 `fetchall()`；固定 200/500 条批量，记录所有行而不跳页。
2. 默认优惠预选以 SQLite `UNION` 游标提供唯一 event ID，每次最多 900 ID；用 `heapq.nlargest` 只留 1500 个轻量候选，不排序全量 Python 字典。
3. 回归测试保留商品准入和结果排序，线上确认服务读到新代码并完成一轮采集。

## Validation and acceptance

- `/Applications/work/gitRepo/opportunity-workbench/.venv/bin/python -B -m unittest discover -s tests`：当前 469 项，2 项 `test_price_admission` 失败；在未改动基线也复现，尚未由本轮修复。
- `test_memory_boundaries.py`：520 条启动迁移、450 条复审跨批次且保留查重结果，2 项通过。
- 线上待执行：公网登录后的默认列表返回 200；网页和 worker `NRestarts=0`、`memory.events` 中 `oom=0`；worker 输出本轮结果并进入 300 秒等待。

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

待线上验收后填写。
